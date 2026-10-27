import logging
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from .const import DOMAIN
from .bridge_entities import get_bridge_entities_for_platform, get_bridge_device_info
_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry, async_add_entities):
    hass.data.setdefault(DOMAIN, {})["async_add_cover"] = async_add_entities

    # 实体编组：窗帘编组（成员全是 cover / kind=curtain）投影成 cover 实体
    try:
        from .entity_group_entities import async_setup_entity_group_covers
        await async_setup_entity_group_covers(hass, async_add_entities)
    except Exception as e:  # noqa: BLE001
        _LOGGER.exception("[entity_group] 编组 cover 初始化失败: %s", e)

    bdi = get_bridge_device_info(entry.entry_id)
    try: be = get_bridge_entities_for_platform(hass, "cover", bdi)
    except Exception as e: _LOGGER.error("[bridge] cover: %s", e); return
    if not be: return
    entities = [ent for _, ent in be]; reg = er.async_get(hass)
    for eid, ent in be: reg.async_get_or_create(domain="cover", platform=DOMAIN, unique_id=ent.unique_id, suggested_object_id=eid.split(".", 1)[1])
    rb = hass.data.setdefault(DOMAIN, {}).setdefault("bridge_entity_instances", {})
    for eid, ent in be: rb[eid] = ent
    async_add_entities(entities)
    _LOGGER.info("[bridge] cover 创建 %d 个", len(be))
