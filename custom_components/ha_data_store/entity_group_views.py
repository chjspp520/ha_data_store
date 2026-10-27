"""实体编组的 HTTP 接口（薄视图层）。

统一前缀 ``/api/ha_data_store``。视图只做四件事：鉴权 / 参数解析 / 调存储层 / 异常转状态码。
**所有未预期异常都会带上 ``type: message`` 回传给前端并在日志里打完整堆栈**，
避免再出现"只看到 HTTP 500、不知道哪里错"的情况。

路由一览：
    GET    /entity_groups                     列表（id/kind/entity/q/enabled/... ）
    POST   /entity_groups                     新建
    GET    /entity_groups/version             版本号（脏检查）
    POST   /entity_groups/batch               批量 upsert / 整库导入
    POST   /entity_groups/order               编组排序
    GET    /entity_groups/by-entity/{eid}     反查实体所属编组
    GET    /entity_groups/{gid}               详情
    PUT    /entity_groups/{gid}               全量更新（members 传了才整组替换；带新 id 即改名）
    PATCH  /entity_groups/{gid}               局部更新（members 缺省不动）
    DELETE /entity_groups/{gid}               删除（成员级联）
    GET    /entity_groups/{gid}/members       成员列表（?entity= 查单个 / ?domain= 过滤）
    POST   /entity_groups/{gid}/members       批量新增（幂等 upsert）
    PUT    /entity_groups/{gid}/members?entity=   整体替换单个成员
    PATCH  /entity_groups/{gid}/members?entity=   局部更新单个成员（extra 浅合并）
    DELETE /entity_groups/{gid}/members?entity=   移除单个成员
    POST   /entity_groups/{gid}/members/remove    批量移除
    POST   /entity_groups/{gid}/members/order     成员排序
"""
from __future__ import annotations

import functools
import logging
import traceback
from typing import Any

import aiohttp.web as web
from homeassistant.core import HomeAssistant

from .entity_group_store import GROUP_ID_RE, EntityGroupError, EntityGroupStore
from .http_api import _BaseDBView

_LOGGER = logging.getLogger(__name__)

_GID = "{group_id:eg_[a-z0-9_]+}"     # aiohttp 的路径变量正则不能含 {n,m}
_PREFIX = "/api/ha_data_store/entity_groups"


def _as_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("0", "false", "no", "off", "")


def _split(value: str | None) -> list[str] | None:
    if not value:
        return None
    items = [v.strip() for v in value.split(",") if v.strip()]
    return items or None


def _json_body_error(detail: str) -> web.Response:
    return web.json_response(
        {"success": False, "error": "invalid_param", "detail": detail}, status=400
    )


def _guarded(fn):
    """最外层兜底：把任何未捕获异常变成带 traceback 的 JSON 500。

    没有它的话，aiohttp 只会回 ``500 Internal Server Error / Server got itself in
    trouble``，前端完全看不到根因（本次 \"HTTP 500\" 排查就是这么卡住的）。
    """

    @functools.wraps(fn)
    async def wrapper(self, request, *args, **kwargs):
        # 部分 HA/aiohttp 组合会把 URL 路径变量当关键字参数传进来
        # （例如 put(request, group_id="eg_xxx")）。本模块统一只从
        # ``request.match_info`` 取路径变量，所以这里**只传 request**，
        # 不要把 kwargs 透传下去，否则会 500：
        #   TypeError: EntityGroupItemView.put() got an unexpected keyword argument 'group_id'
        try:
            return await fn(self, request)
        except web.HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception(
                "[entity_group] %s 未捕获异常（path=%s）", fn.__name__, request.path
            )
            return web.json_response(
                {
                    "success": False,
                    "error": "internal_error",
                    "detail": f"{type(exc).__name__}: {exc}",
                    "operation": fn.__name__,
                    "path": request.path,
                    "match_info": dict(request.match_info),
                    "traceback": traceback.format_exc().strip().splitlines()[-10:],
                },
                status=500,
            )

    wrapper._eg_guarded = True
    return wrapper


class _BaseGroupView(_BaseDBView):
    """编组视图公共部分：鉴权 / 错误转换 / 写后通知。"""

    def __init_subclass__(cls, **kwargs):
        """给每个子类的 get/post/put/patch/delete 自动套一层异常兜底。"""
        super().__init_subclass__(**kwargs)
        for method_name in ("get", "post", "put", "patch", "delete"):
            fn = cls.__dict__.get(method_name)
            if fn is not None and not getattr(fn, "_eg_guarded", False):
                setattr(cls, method_name, _guarded(fn))

    def store(self) -> EntityGroupStore:
        return EntityGroupStore(self._db_path)

    # -- 鉴权 -----------------------------------------------------------
    # 带 ?key= 时走 API Key 校验（与其它数据接口一致）；不带时按管理页惯例
    # 只校验总开关，方便 HA 前端面板直接用登录态调用。
    def check_read(self, request: web.Request) -> web.Response | None:
        if request.query.get("key") or request.headers.get("Authorization"):
            return self._check_api_enabled(request)
        return self._check_master_switch(request.app["hass"])

    def check_write(self, request: web.Request) -> web.Response | None:
        if (resp := self.check_read(request)):
            return resp
        return self._check_db_edit_enabled(request.app["hass"])

    # -- 响应 -----------------------------------------------------------
    def ok(self, **payload: Any) -> web.Response:
        body = {"success": True}
        body.update(payload)
        return self.json(body)

    def fail(self, error: str, detail: str = "", status: int = 400,
             **extra: Any) -> web.Response:
        body = {"success": False, "error": error, "detail": detail}
        body.update(extra)
        return self.json(body, status_code=status)

    def error_response(self, exc: EntityGroupError) -> web.Response:
        return self.fail(exc.error, exc.detail, exc.status, **exc.extra)

    def _path_var(self, request: web.Request, name: str) -> tuple[str, web.Response | None]:
        """安全取路径变量；缺失时返回 400 并把真实 match_info 带回去（排查路由用）。"""
        value = request.match_info.get(name)
        if value:
            return value, None
        return "", self.fail(
            "invalid_param",
            f"路径缺少变量 {name}（match_info={dict(request.match_info)}）",
        )

    def bad_group_id(self, group_id: str) -> web.Response | None:
        if GROUP_ID_RE.match(group_id or ""):
            return None
        return self.fail(
            "invalid_param",
            f"编组 id 格式非法（eg_ + 2~32 位小写字母/数字/下划线）: {group_id}",
        )

    # -- 统一的执行 + 异常兜底 -----------------------------------------
    async def _call(self, hass: HomeAssistant, func, *args):
        """在 executor 里执行存储层方法，返回 ``(result, error_response)``。

        - ``EntityGroupError`` → 按自带 error/status 转 JSON（400/404/409）
        - 其它异常 → 打完整堆栈 + 回 ``500 internal_error``，
          body 里带 ``exception`` 与 ``detail``，前端能直接看到根因。
        """
        try:
            result = await self._exec_in_executor(hass, func, *args)
            return result, None
        except EntityGroupError as exc:
            return None, self.error_response(exc)
        except Exception as exc:  # noqa: BLE001
            name = getattr(func, "__name__", str(func))
            _LOGGER.exception("[entity_group] %s 执行失败（参数: %r）", name, args)
            return None, self.fail(
                "internal_error",
                f"{type(exc).__name__}: {exc}",
                500,
                exception=type(exc).__name__,
                operation=name,
                # 便于在浏览器 Network 面板直接看到根因，不必去翻 HA 日志
                traceback=(traceback.format_exc().strip().splitlines()[-8:]),
            )

    # -- 写后通知 -------------------------------------------------------
    def notify(self, hass: HomeAssistant) -> None:
        try:
            from .entity_group_entities import async_notify_groups_changed

            async_notify_groups_changed(hass)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("[entity_group] 通知实体同步失败", exc_info=True)

    # -- 请求体 ---------------------------------------------------------
    @staticmethod
    async def _json_body(request: web.Request) -> tuple[Any, web.Response | None]:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return None, _json_body_error("请求体不是合法 JSON")
        return body, None

    @staticmethod
    async def _json_object(request: web.Request) -> tuple[dict, web.Response | None]:
        body, err = await _BaseGroupView._json_body(request)
        if err is not None:
            return {}, err
        if not isinstance(body, dict):
            return {}, _json_body_error("请求体需为 JSON 对象")
        return body, None


# ── 编组层 ─────────────────────────────────────────────────────────────────
class EntityGroupsView(_BaseGroupView):
    """GET 列表 / POST 新建。"""

    url = _PREFIX
    name = "api:ha_data_store:entity_groups"

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        if (resp := self.check_read(request)):
            return resp

        q = request.query
        enabled_raw = (q.get("enabled") or "1").strip().lower()
        enabled: Any = None if enabled_raw in ("all", "*", "") else _as_bool(enabled_raw)
        try:
            limit = int(q["limit"]) if q.get("limit") else None
            offset = int(q.get("offset") or 0)
        except ValueError:
            return self.fail("invalid_param", "limit/offset 必须是整数")

        store = self.store()
        result, err = await self._call(
            hass, store.list_groups,
            _split(q.get("id") or q.get("ids")),
            _split(q.get("kind") or q.get("kinds")),
            q.get("entity") or None,
            q.get("q") or q.get("keyword") or None,
            enabled,
            _as_bool(q.get("include_members"), True),
            q.get("sort") or "sort",
            limit,
            offset,
        )
        if err is not None:
            return err
        version, err2 = await self._call(hass, store.get_version)
        if err2 is not None:
            return err2
        return self.ok(groups=result["groups"], total=result["total"],
                       version=version["version"])

    async def post(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        if (resp := self.check_write(request)):
            return resp
        body, err = await self._json_object(request)
        if err is not None:
            return err
        result, err = await self._call(hass, self.store().create_group, body)
        if err is not None:
            return err
        self.notify(hass)
        return self.json({"success": True, **result}, status_code=201)


class EntityGroupVersionView(_BaseGroupView):
    """GET 版本号（几十字节，供前端脏检查）。"""

    url = f"{_PREFIX}/version"
    name = "api:ha_data_store:entity_groups_version"

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        if (resp := self.check_read(request)):
            return resp
        info, err = await self._call(hass, self.store().get_version)
        if err is not None:
            return err
        return self.ok(**info)


class EntityGroupBatchView(_BaseGroupView):
    """POST 批量保存：mode=upsert（默认）| replace。"""

    url = f"{_PREFIX}/batch"
    name = "api:ha_data_store:entity_groups_batch"

    async def post(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        if (resp := self.check_write(request)):
            return resp
        body, err = await self._json_object(request)
        if err is not None:
            return err
        result, err = await self._call(
            hass, self.store().batch_save,
            body.get("mode") or "upsert",
            body.get("groups"),
            body.get("deleted"),
        )
        if err is not None:
            return err
        self.notify(hass)
        return self.ok(**result)


class EntityGroupOrderView(_BaseGroupView):
    """POST 编组排序：body {"order": ["eg_a", "eg_b"]}。"""

    url = f"{_PREFIX}/order"
    name = "api:ha_data_store:entity_groups_order"

    async def post(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        if (resp := self.check_write(request)):
            return resp
        body, err = await self._json_object(request)
        if err is not None:
            return err
        result, err = await self._call(hass, self.store().reorder, body.get("order"))
        if err is not None:
            return err
        self.notify(hass)
        return self.ok(**result)


class EntityGroupByEntityView(_BaseGroupView):
    """GET 反查某实体属于哪些编组。"""

    url = f"{_PREFIX}/by-entity/{{entity_id}}"
    name = "api:ha_data_store:entity_groups_by_entity"

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        if (resp := self.check_read(request)):
            return resp
        entity_id, err = self._path_var(request, "entity_id")
        if err is not None:
            return err
        groups, err = await self._call(
            hass, self.store().find_groups_by_entity, entity_id
        )
        if err is not None:
            return err
        return self.ok(groups=groups, total=len(groups))


class EntityGroupItemView(_BaseGroupView):
    """GET / PUT / PATCH / DELETE 单个编组。"""

    url = f"{_PREFIX}/{_GID}"
    name = "api:ha_data_store:entity_group_item"

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        if (resp := self.check_read(request)):
            return resp
        group_id, err = self._path_var(request, "group_id")
        if err is not None:
            return err
        if (resp := self.bad_group_id(group_id)):
            return resp
        with_members = _as_bool(request.query.get("include_members"), True)
        store = self.store()
        group, err = await self._call(hass, store.get_group, group_id, with_members)
        if err is not None:
            return err
        if group is None:
            return self.fail("not_found", f"编组不存在: {group_id}", 404)
        version, err = await self._call(hass, store.get_version)
        if err is not None:
            return err
        return self.ok(group=group, version=version["version"])

    async def put(self, request: web.Request) -> web.Response:
        return await self._write(request, full=True)

    async def patch(self, request: web.Request) -> web.Response:
        return await self._write(request, full=False)

    async def _write(self, request: web.Request, full: bool) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        if (resp := self.check_write(request)):
            return resp
        group_id, err = self._path_var(request, "group_id")
        if err is not None:
            return err
        if (resp := self.bad_group_id(group_id)):
            return resp
        body, err = await self._json_object(request)
        if err is not None:
            return err

        rev = request.query.get("rev") or body.get("rev")
        store = self.store()
        func = store.update_group if full else store.patch_group
        result, err = await self._call(hass, func, group_id, body, rev)
        if err is not None:
            return err
        self.notify(hass)
        return self.ok(**result)

    async def delete(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        if (resp := self.check_write(request)):
            return resp
        group_id, err = self._path_var(request, "group_id")
        if err is not None:
            return err
        if (resp := self.bad_group_id(group_id)):
            return resp
        result, err = await self._call(hass, self.store().delete_group, group_id)
        if err is not None:
            return err
        self.notify(hass)
        return self.ok(**result)


# ── 成员层 ─────────────────────────────────────────────────────────────────
class EntityGroupMembersView(_BaseGroupView):
    """GET 列表 / POST 新增 / PUT·PATCH·DELETE 单个成员（?entity=）。"""

    url = f"{_PREFIX}/{_GID}/members"
    name = "api:ha_data_store:entity_group_members"

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        if (resp := self.check_read(request)):
            return resp
        group_id, err = self._path_var(request, "group_id")
        if err is not None:
            return err
        if (resp := self.bad_group_id(group_id)):
            return resp
        members, err = await self._call(
            hass, self.store().list_members, group_id
        )
        if err is not None:
            return err

        entity_id = request.query.get("entity")
        if entity_id:
            hit = [m for m in members if m["entity_id"] == entity_id]
            return self.ok(member=hit[0] if hit else None, members=members)

        domain = request.query.get("domain")
        if domain:
            members = [m for m in members if m["entity_id"].startswith(f"{domain}.")]
        return self.ok(members=members, total=len(members))

    async def post(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        if (resp := self.check_write(request)):
            return resp
        group_id, err = self._path_var(request, "group_id")
        if err is not None:
            return err
        if (resp := self.bad_group_id(group_id)):
            return resp
        body, err = await self._json_body(request)
        if err is not None:
            return err
        result, err = await self._call(hass, self.store().add_members, group_id, body)
        if err is not None:
            return err
        self.notify(hass)
        return self.ok(**result)

    async def put(self, request: web.Request) -> web.Response:
        return await self._single(request, "put")

    async def patch(self, request: web.Request) -> web.Response:
        return await self._single(request, "patch")

    async def delete(self, request: web.Request) -> web.Response:
        return await self._single(request, "delete")

    async def _single(self, request: web.Request, action: str) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        if (resp := self.check_write(request)):
            return resp
        group_id, err = self._path_var(request, "group_id")
        if err is not None:
            return err
        if (resp := self.bad_group_id(group_id)):
            return resp
        entity_id = request.query.get("entity")
        if not entity_id:
            return self.fail("invalid_param", "缺少 query 参数 entity")

        store = self.store()
        body: dict = {}
        if action != "delete":
            body, err = await self._json_object(request)
            if err is not None:
                return err

        if action == "put":
            result, err = await self._call(
                hass, store.update_member, group_id, entity_id, body
            )
        elif action == "patch":
            result, err = await self._call(
                hass, store.patch_member, group_id, entity_id, body
            )
        else:
            result, err = await self._call(
                hass, store.remove_member, group_id, entity_id
            )
        if err is not None:
            return err
        self.notify(hass)
        return self.ok(**result)


class EntityGroupMembersRemoveView(_BaseGroupView):
    """POST 批量移除成员：body {"entities": [...]}。"""

    url = f"{_PREFIX}/{_GID}/members/remove"
    name = "api:ha_data_store:entity_group_members_remove"

    async def post(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        if (resp := self.check_write(request)):
            return resp
        group_id, err = self._path_var(request, "group_id")
        if err is not None:
            return err
        if (resp := self.bad_group_id(group_id)):
            return resp
        body, err = await self._json_object(request)
        if err is not None:
            return err
        entities = body.get("entities") or body.get("entity_ids") or body.get("members")
        result, err = await self._call(
            hass, self.store().remove_members, group_id, entities
        )
        if err is not None:
            return err
        self.notify(hass)
        return self.ok(**result)


class EntityGroupMembersOrderView(_BaseGroupView):
    """POST 成员排序：body {"order": ["light.a", "light.b"]}。"""

    url = f"{_PREFIX}/{_GID}/members/order"
    name = "api:ha_data_store:entity_group_members_order"

    async def post(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        if (resp := self.check_write(request)):
            return resp
        group_id, err = self._path_var(request, "group_id")
        if err is not None:
            return err
        if (resp := self.bad_group_id(group_id)):
            return resp
        body, err = await self._json_object(request)
        if err is not None:
            return err
        result, err = await self._call(
            hass, self.store().reorder_members, group_id, body.get("order")
        )
        if err is not None:
            return err
        self.notify(hass)
        return self.ok(**result)


class EntityGroupDiagView(_BaseGroupView):
    """GET 自检：逐个探测存储层方法，定位"哪个接口/哪个方法在 500"。"""

    url = f"{_PREFIX}/_diag"
    name = "api:ha_data_store:entity_groups_diag"

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        if (resp := self.check_read(request)):
            return resp

        store = self.store()
        out: dict[str, Any] = {
            "db_path": self._db_path,
            "db_exists": __import__("os").path.exists(self._db_path),
            "view_class": type(self).__name__,
            "match_info": dict(request.match_info),
            "read_state": {
                "api_enabled": hass.data.get("ha_data_store", {}).get("api_enabled"),
                "db_edit_enabled": hass.data.get("ha_data_store", {}).get("db_edit_enabled"),
            },
        }

        async def probe(name: str, *args) -> Any:
            fn = getattr(store, name, None)
            if fn is None:
                out[name] = "MISSING"
                return None
            try:
                res = await self._exec_in_executor(hass, fn, *args)
                out[name] = "ok"
                return res
            except Exception as exc:  # noqa: BLE001
                out[name] = f"{type(exc).__name__}: {exc}"
                return None

        ver = await probe("get_version")
        if ver is not None:
            out["version"] = ver.get("version")
        groups_res = await probe("list_groups")
        gid = None
        if groups_res is not None:
            out["group_total"] = groups_res.get("total")
            if groups_res.get("groups"):
                gid = groups_res["groups"][0]["id"]
        await probe("find_groups_by_entity", "input_boolean.ce_shi")
        if gid:
            out["probe_group_id"] = gid
            group = await probe("get_group", gid, True)
            if group is not None:
                out["group_members"] = len(group.get("members") or [])
            await probe("list_members", gid)

        return self.ok(**out)


# ── 注册 ───────────────────────────────────────────────────────────────────
VIEWS = (
    EntityGroupsView,
    EntityGroupVersionView,
    EntityGroupDiagView,
    EntityGroupBatchView,
    EntityGroupOrderView,
    EntityGroupByEntityView,
    EntityGroupItemView,
    EntityGroupMembersView,
    EntityGroupMembersRemoveView,
    EntityGroupMembersOrderView,
)


def register_api_views(hass: HomeAssistant, db_path: str) -> None:
    """由 ``__init__._register_api_views`` 调用。

    注意顺序：静态路径（version / batch / order / by-entity）先注册，
    带正则变量的 item 视图放最后 —— 正则已能排除同名单段，顺序只是第二道保险。
    """
    for view_cls in VIEWS:
        hass.http.register_view(view_cls(db_path))
    _LOGGER.debug("[entity_group] 已注册 %d 个编组接口", len(VIEWS))
