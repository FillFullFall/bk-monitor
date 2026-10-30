"""dimension_values 接口 cluster_id 维度名称补充测试。

验证后端在 dimension_key=cluster_id 时新增 options 字段返回 [{id, name}] 结构，
values 字段仍为原始字符串数组保持向后兼容；BCS API 异常时 options 降级为 name=id，
非 cluster 维度不新增 options 字段。

名称补充复用 BcsApi.get_cluster_by_cluster_id 按 cluster_id 精确查询（与既有
_get_bcs_cluster_name 范式一致），不依赖业务 ID，关联空间的集群也能查到名称。
"""

from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from apps.exceptions import ApiError, ApiRequestError, ApiResultError
from apps.log_search.models import IndexSetTag, LogIndexSet
from apps.log_search.views.scene_search_views import SceneSearchViewSet

# 视图内通过 from apps.api.modules.bcs import BcsApi 调用 get_cluster_by_cluster_id（延迟导入）。
# BcsApi 是 apps/api/__init__.py 里 SimpleLazyObject 包装的 _BcsApi 实例，
# get_cluster_by_cluster_id 是 __init__ 里赋值的实例属性，故 patch 目标指向 apps.api.BcsApi 实例属性。
GET_CLUSTER_PATH = "apps.api.BcsApi.get_cluster_by_cluster_id"


def _setup_k8s_index_set(cluster_ids, space_uid="bkcc__2"):
    """创建带 scene=k8s + cluster_id 标签的索引集。"""
    scene_tag_id = IndexSetTag.get_tag_id(name="scene", value="k8s", tag_type="scene")
    tag_ids = [str(scene_tag_id)]
    for cid in cluster_ids:
        tid = IndexSetTag.get_tag_id(name="cluster_id", value=cid, tag_type="scene")
        tag_ids.append(str(tid))
    return LogIndexSet.objects.create(
        index_set_name="idx_{}".format("_".join(cluster_ids)),
        space_uid=space_uid,
        scenario_id="log",
        tag_ids=tag_ids,
        is_active=True,
    )


def _get_viewset(action_name, request):
    if not isinstance(request, Request):
        request = Request(request, parsers=[JSONParser(), FormParser(), MultiPartParser()])
    vs = SceneSearchViewSet(format_kwarg=None)
    vs.request = request
    vs.kwargs = {}
    vs.action = action_name
    return vs


def _post_request(payload):
    factory = APIRequestFactory()
    return factory.post(
        "/api/v1/search/scene/dimension_values/",
        data=payload,
        format="json",
    )


def _cluster_name_side_effect(name_map):
    """构造 get_cluster_by_cluster_id 的 side_effect：按 cluster_id(大写) 返回 {clusterName}。

    name_map: {原始 cluster_id: 集群名称}；不在映射内的 cluster_id 抛异常模拟 BCS 无此集群。
    """

    def _side_effect(params):
        cid = params["cluster_id"].upper()
        if cid not in name_map:
            # 模拟 BCS 查不到集群：HTTP 成功但业务返回失败（DataAPI 抛 ApiResultError）
            raise ApiResultError("cluster not found: {}".format(cid), code="404")
        return {"clusterName": name_map[cid], "projectID": "proj-{}".format(cid)}

    return _side_effect


@override_settings(PRE_SEARCH_SECONDS=60, TIME_ZONE="UTC")
class TestDimensionValuesClusterName(TestCase):
    @patch(
        "apps.log_search.views.scene_search_views.IndexSetHandler.get_all_related_space_uids",
        return_value=["bkcc__2"],
    )
    @patch(GET_CLUSTER_PATH)
    def test_cluster_id_returns_id_name_pairs(self, mock_bcs, mock_related):
        """cluster_id 维度 + BCS 正常返回 → options 为 [{id, name}]，values 仍为字符串数组。"""
        _setup_k8s_index_set(["cls-001", "cls-002"])
        mock_bcs.side_effect = _cluster_name_side_effect({"CLS-001": "生产集群", "CLS-002": "测试集群"})

        req = _post_request({"bk_biz_id": 2, "scene": "k8s", "dimension_key": "cluster_id"})
        response = _get_viewset("dimension_values", req).dimension_values(req)

        self.assertEqual(response.status_code, 200)
        # values 仍为原始字符串数组（向后兼容）
        self.assertEqual(response.data["values"], ["cls-001", "cls-002"])
        # options 为 [{id, name}] 结构
        options = response.data["options"]
        self.assertIsInstance(options, list)
        self.assertEqual(len(options), 2)
        option_map = {o["id"]: o["name"] for o in options}
        self.assertEqual(option_map["cls-001"], "生产集群")
        self.assertEqual(option_map["cls-002"], "测试集群")

    @patch(
        "apps.log_search.views.scene_search_views.IndexSetHandler.get_all_related_space_uids",
        return_value=["bkcc__2"],
    )
    @patch(GET_CLUSTER_PATH)
    def test_cluster_id_unknown_id_falls_back_to_id(self, mock_bcs, mock_related):
        """BCS 无该 cluster_id（查询抛异常）→ options 中 name 回退为 id 本身。"""
        _setup_k8s_index_set(["cls-unknown"])
        # BCS 只知道 CLS-001，查 CLS-UNKNOWN 时抛异常 → 降级为 id
        mock_bcs.side_effect = _cluster_name_side_effect({"CLS-001": "生产集群"})

        req = _post_request({"bk_biz_id": 2, "scene": "k8s", "dimension_key": "cluster_id"})
        response = _get_viewset("dimension_values", req).dimension_values(req)

        self.assertEqual(response.data["values"], ["cls-unknown"])
        options = response.data["options"]
        self.assertEqual(len(options), 1)
        self.assertEqual(options[0]["id"], "cls-unknown")
        self.assertEqual(options[0]["name"], "cls-unknown")

    @patch(
        "apps.log_search.views.scene_search_views.IndexSetHandler.get_all_related_space_uids",
        return_value=["bkcc__2"],
    )
    @patch(GET_CLUSTER_PATH)
    def test_cluster_id_bcs_api_exception_falls_back(self, mock_bcs, mock_related):
        """BCS API 网关异常（ApiRequestError）→ options 降级为 name=id，不报错。"""
        _setup_k8s_index_set(["cls-err"])
        mock_bcs.side_effect = ApiRequestError("BCS gateway timeout", "req-id")

        req = _post_request({"bk_biz_id": 2, "scene": "k8s", "dimension_key": "cluster_id"})
        response = _get_viewset("dimension_values", req).dimension_values(req)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["values"], ["cls-err"])
        options = response.data["options"]
        self.assertEqual(len(options), 1)
        self.assertEqual(options[0]["id"], "cls-err")
        self.assertEqual(options[0]["name"], "cls-err")

    @patch(
        "apps.log_search.views.scene_search_views.IndexSetHandler.get_all_related_space_uids",
        return_value=["bkcc__2"],
    )
    @patch(GET_CLUSTER_PATH)
    def test_cluster_id_empty_values_skips_bcs_call(self, mock_bcs, mock_related):
        """无匹配索引集 → values 为空，无 options 字段，不调用 BCS API。"""
        mock_bcs.side_effect = _cluster_name_side_effect({})

        req = _post_request({"bk_biz_id": 2, "scene": "k8s", "dimension_key": "cluster_id"})
        response = _get_viewset("dimension_values", req).dimension_values(req)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["values"], [])
        self.assertNotIn("options", response.data)
        mock_bcs.assert_not_called()

    @patch(
        "apps.log_search.views.scene_search_views.IndexSetHandler.get_all_related_space_uids",
        return_value=["bkcc__2"],
    )
    def test_non_cluster_dimension_returns_strings(self, mock_related):
        """stream 维度 → 仍返回纯字符串数组，无 options 字段。"""
        scene_tag_id = IndexSetTag.get_tag_id(name="scene", value="k8s", tag_type="scene")
        stdout_tag = IndexSetTag.get_tag_id(name="stream", value="stdout", tag_type="scene")
        LogIndexSet.objects.create(
            index_set_name="idx_stream",
            space_uid="bkcc__2",
            scenario_id="log",
            tag_ids=[str(scene_tag_id), str(stdout_tag)],
            is_active=True,
        )

        req = _post_request({"bk_biz_id": 2, "scene": "k8s", "dimension_key": "stream"})
        response = _get_viewset("dimension_values", req).dimension_values(req)

        self.assertEqual(response.status_code, 200)
        values = response.data["values"]
        self.assertEqual(values, ["stdout"])
        for v in values:
            self.assertIsInstance(v, str)
        self.assertNotIn("options", response.data)

    @patch(
        "apps.log_search.views.scene_search_views.IndexSetHandler.get_all_related_space_uids",
        return_value=["bkcc__2", "bcs__BCS-K8S-001"],
    )
    @patch(GET_CLUSTER_PATH)
    def test_cluster_id_with_filters_enriches_filtered_results(self, mock_bcs, mock_related):
        """级联筛选后仅对筛选结果补充名称。"""
        scene_tag = IndexSetTag.get_tag_id(name="scene", value="k8s", tag_type="scene")
        stdout_tag = IndexSetTag.get_tag_id(name="stream", value="stdout", tag_type="scene")
        file_tag = IndexSetTag.get_tag_id(name="stream", value="file", tag_type="scene")
        c1 = IndexSetTag.get_tag_id(name="cluster_id", value="cls-stdout", tag_type="scene")
        c2 = IndexSetTag.get_tag_id(name="cluster_id", value="cls-file", tag_type="scene")

        LogIndexSet.objects.create(
            index_set_name="idx_stdout",
            space_uid="bcs__BCS-K8S-001",
            scenario_id="log",
            tag_ids=[str(scene_tag), str(stdout_tag), str(c1)],
            is_active=True,
        )
        LogIndexSet.objects.create(
            index_set_name="idx_file",
            space_uid="bcs__BCS-K8S-001",
            scenario_id="log",
            tag_ids=[str(scene_tag), str(file_tag), str(c2)],
            is_active=True,
        )
        mock_bcs.side_effect = _cluster_name_side_effect(
            {"CLS-STDOUT": "标准输出集群", "CLS-FILE": "文件集群"}
        )

        req = _post_request(
            {
                "bk_biz_id": 2,
                "scene": "k8s",
                "dimension_key": "cluster_id",
                "filters": [{"field_name": "stream", "value": ["stdout"], "op": "eq"}],
            }
        )
        response = _get_viewset("dimension_values", req).dimension_values(req)

        self.assertEqual(response.data["values"], ["cls-stdout"])
        options = response.data["options"]
        self.assertEqual(len(options), 1)
        self.assertEqual(options[0]["id"], "cls-stdout")
        self.assertEqual(options[0]["name"], "标准输出集群")

    @patch(
        "apps.log_search.views.scene_search_views.IndexSetHandler.get_all_related_space_uids",
        return_value=["bkcc__2"],
    )
    @patch(GET_CLUSTER_PATH)
    def test_cluster_id_sorted_by_id(self, mock_bcs, mock_related):
        """多个 cluster_id → options 按 id 字典序排列。"""
        _setup_k8s_index_set(["cls-c", "cls-a", "cls-b"])
        # 全部查询失败（业务失败）→ 全部降级为 id，仍按字典序排列
        mock_bcs.side_effect = ApiResultError("cluster not found", code="404")

        req = _post_request({"bk_biz_id": 2, "scene": "k8s", "dimension_key": "cluster_id"})
        response = _get_viewset("dimension_values", req).dimension_values(req)

        ids = [o["id"] for o in response.data["options"]]
        self.assertEqual(ids, ["cls-a", "cls-b", "cls-c"])

    @patch(
        "apps.log_search.views.scene_search_views.IndexSetHandler.get_all_related_space_uids",
        return_value=["bkcc__2", "bcs__BCS-K8S-001"],
    )
    @patch(GET_CLUSTER_PATH)
    def test_cluster_id_related_space_enrichment(self, mock_bcs, mock_related):
        """关联空间的 cluster_id 也被补充名称。

        get_cluster_by_cluster_id 按 cluster_id 精确查询、不依赖业务 ID，
        因此来自关联 BCS 空间的 cluster_id 同样能查到名称。
        """
        scene_tag = IndexSetTag.get_tag_id(name="scene", value="k8s", tag_type="scene")
        biz_tag = IndexSetTag.get_tag_id(name="cluster_id", value="cls-biz", tag_type="scene")
        related_tag = IndexSetTag.get_tag_id(name="cluster_id", value="cls-related", tag_type="scene")

        LogIndexSet.objects.create(
            index_set_name="idx_biz",
            space_uid="bkcc__2",
            scenario_id="log",
            tag_ids=[str(scene_tag), str(biz_tag)],
            is_active=True,
        )
        LogIndexSet.objects.create(
            index_set_name="idx_related",
            space_uid="bcs__BCS-K8S-001",
            scenario_id="log",
            tag_ids=[str(scene_tag), str(related_tag)],
            is_active=True,
        )
        mock_bcs.side_effect = _cluster_name_side_effect({"CLS-BIZ": "业务集群", "CLS-RELATED": "关联集群"})

        req = _post_request({"bk_biz_id": 2, "scene": "k8s", "dimension_key": "cluster_id"})
        response = _get_viewset("dimension_values", req).dimension_values(req)

        option_map = {o["id"]: o["name"] for o in response.data["options"]}
        self.assertEqual(option_map["cls-biz"], "业务集群")
        self.assertEqual(option_map["cls-related"], "关联集群")

    @patch(
        "apps.log_search.views.scene_search_views.IndexSetHandler.get_all_related_space_uids",
        return_value=["bkcc__2"],
    )
    @patch(GET_CLUSTER_PATH)
    def test_view_response_format_cluster_id(self, mock_bcs, mock_related):
        """视图层集成：响应体结构为 {dimension_key, values: [str], options: [{id, name}]}。"""
        _setup_k8s_index_set(["cls-fmt"])
        mock_bcs.side_effect = _cluster_name_side_effect({"CLS-FMT": "格式集群"})

        req = _post_request({"bk_biz_id": 2, "scene": "k8s", "dimension_key": "cluster_id"})
        response = _get_viewset("dimension_values", req).dimension_values(req)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["dimension_key"], "cluster_id")
        self.assertEqual(response.data["values"], ["cls-fmt"])
        self.assertIsInstance(response.data["options"], list)
        for item in response.data["options"]:
            self.assertIn("id", item)
            self.assertIn("name", item)

    @patch(
        "apps.log_search.views.scene_search_views.IndexSetHandler.get_all_related_space_uids",
        return_value=["bkcc__2"],
    )
    def test_view_response_format_non_cluster(self, mock_related):
        """视图层集成：非 cluster 维度响应体 values 为字符串数组，无 options 字段。"""
        scene_tag_id = IndexSetTag.get_tag_id(name="scene", value="k8s", tag_type="scene")
        ns_tag = IndexSetTag.get_tag_id(name="namespace", value="default", tag_type="scene")
        LogIndexSet.objects.create(
            index_set_name="idx_ns",
            space_uid="bkcc__2",
            scenario_id="log",
            tag_ids=[str(scene_tag_id), str(ns_tag)],
            is_active=True,
        )

        req = _post_request({"bk_biz_id": 2, "scene": "k8s", "dimension_key": "namespace"})
        response = _get_viewset("dimension_values", req).dimension_values(req)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["dimension_key"], "namespace")
        for v in response.data["values"]:
            self.assertIsInstance(v, str)
        self.assertNotIn("options", response.data)

    @patch(
        "apps.log_search.views.scene_search_views.IndexSetHandler.get_all_related_space_uids",
        return_value=["bkcc__2"],
    )
    @patch(GET_CLUSTER_PATH)
    def test_cluster_id_empty_name_falls_back_to_id(self, mock_bcs, mock_related):
        """BCS 返回 clusterName 为空串 → name 降级为 id 本身。"""
        _setup_k8s_index_set(["cls-noname"])
        mock_bcs.side_effect = _cluster_name_side_effect({"CLS-NONAME": ""})

        req = _post_request({"bk_biz_id": 2, "scene": "k8s", "dimension_key": "cluster_id"})
        response = _get_viewset("dimension_values", req).dimension_values(req)

        options = response.data["options"]
        self.assertEqual(len(options), 1)
        self.assertEqual(options[0]["id"], "cls-noname")
        self.assertEqual(options[0]["name"], "cls-noname")

    @patch(
        "apps.log_search.views.scene_search_views.IndexSetHandler.get_all_related_space_uids",
        return_value=["bkcc__2"],
    )
    @patch(GET_CLUSTER_PATH)
    def test_cluster_id_none_name_falls_back_to_id(self, mock_bcs, mock_related):
        """BCS 返回 clusterName 为 None（字段缺失/null）→ name 降级为 id 本身。"""
        _setup_k8s_index_set(["cls-nullname"])

        def _none_name_side_effect(params):
            # 模拟 BCS 返回该集群但 clusterName 字段为 null
            assert params["cluster_id"].upper() == "CLS-NULLNAME"
            return {"clusterName": None, "projectID": "proj"}

        mock_bcs.side_effect = _none_name_side_effect

        req = _post_request({"bk_biz_id": 2, "scene": "k8s", "dimension_key": "cluster_id"})
        response = _get_viewset("dimension_values", req).dimension_values(req)

        options = response.data["options"]
        self.assertEqual(len(options), 1)
        self.assertEqual(options[0]["id"], "cls-nullname")
        self.assertEqual(options[0]["name"], "cls-nullname")

    @patch(
        "apps.log_search.views.scene_search_views.IndexSetHandler.get_all_related_space_uids",
        return_value=["bkcc__2"],
    )
    @patch(GET_CLUSTER_PATH)
    def test_cluster_id_partial_success_mixed(self, mock_bcs, mock_related):
        """混合场景：成功/业务失败/网关失败并存 → 互不影响，各自正确。

        验证 except ApiError 覆盖两个子类（ApiResultError + ApiRequestError），
        且单个失败不减少 result 条数、不影响其他集群名称。
        """
        _setup_k8s_index_set(["cls-ok", "cls-bizfail", "cls-netfail"])

        def _mixed_side_effect(params):
            cid = params["cluster_id"].upper()
            if cid == "CLS-OK":
                return {"clusterName": "正常集群", "projectID": "p"}
            if cid == "CLS-BIZFAIL":
                raise ApiResultError("cluster not found", code="404")
            if cid == "CLS-NETFAIL":
                raise ApiRequestError("gateway timeout", "req-id")
            return {"clusterName": "", "projectID": ""}

        mock_bcs.side_effect = _mixed_side_effect

        req = _post_request({"bk_biz_id": 2, "scene": "k8s", "dimension_key": "cluster_id"})
        response = _get_viewset("dimension_values", req).dimension_values(req)

        self.assertEqual(response.status_code, 200)
        options = response.data["options"]
        # 3 个集群全部返回，失败不影响全局
        self.assertEqual(len(options), 3)
        option_map = {o["id"]: o["name"] for o in options}
        self.assertEqual(option_map["cls-ok"], "正常集群")
        self.assertEqual(option_map["cls-bizfail"], "cls-bizfail")
        self.assertEqual(option_map["cls-netfail"], "cls-netfail")
        # 排序仍正确
        ids = [o["id"] for o in options]
        self.assertEqual(ids, ["cls-bizfail", "cls-netfail", "cls-ok"])

    @patch(
        "apps.log_search.views.scene_search_views.IndexSetHandler.get_all_related_space_uids",
        return_value=["bkcc__2"],
    )
    @patch(GET_CLUSTER_PATH)
    def test_cluster_id_non_api_error_not_swallowed(self, mock_bcs, mock_related):
        """非 ApiError 家族异常（编程错误，如 ValueError）不被吞掉，向上抛出。

        验证异常收窄为 except ApiError 后，不会误吞编程错误。
        """
        _setup_k8s_index_set(["cls-bug"])
        mock_bcs.side_effect = ValueError("unexpected programming error")

        req = _post_request({"bk_biz_id": 2, "scene": "k8s", "dimension_key": "cluster_id"})

        with self.assertRaises(ValueError):
            _get_viewset("dimension_values", req).dimension_values(req)
