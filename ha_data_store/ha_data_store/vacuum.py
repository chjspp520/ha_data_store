import logging
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    # 存储回调，供虚拟设备动态创建
    hass.data.setdefault(DOMAIN, {})["async_add_vacuum"] = async_add_entities
