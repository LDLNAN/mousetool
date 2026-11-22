"""Brand HID driver package and unified device status dispatcher."""
import os

from .base import (
    DEVICE_STATE_CACHE,
    HIDIOCGFEATURE,
    HIDIOCGFEATURE_91,
    HIDIOCGFEATURE_N,
    HIDIOCSFEATURE,
    HIDIOCSFEATURE_91,
    HIDIOCSFEATURE_N,
    LOD_NAMES,
    LOD_PRESETS,
    Asleep,
    _cache_key,
    _ioc,
    _load_device_cache,
    _open_notify,
    _save_device_cache,
)
from .egg import (
    EGG_CFG_LEN,
    EGG_CMD_LEN,
    EGG_DEVICES,
    EGG_GLASS_LOD,
    EGG_POLL_RATES,
    EGG_VID,
    EGG_WE_DEVICES,
    egg8k_read_status,
    egg_blob_ok,
    egg_clamp_cpi,
    egg_format_version,
    egg_parse_config,
    egg_read_config,
    egg_read_version,
    egg_send_cmd,
    egg_serialize_config,
    egg_update_config,
    egg_write_config,
)
from .glorious import (
    CORE2_RATES,
    GLORIOUS_CLASSIC,
    GLORIOUS_CORE2,
    GLORIOUS_PIXART,
    GLORIOUS_SINOWEALTH,
    SINO_RATES,
    SINO_RATES_INV,
    SINO_SENSORS,
    classic_read_status,
    core2_read_status,
    core2_xfer,
    pixart_read_status,
    pixart_write_config,
    sino_parse_block,
    sino_read_block,
)
from .logitech import (
    HIDPP_LONG,
    HIDPP_SHORT,
    HIDPP_SW,
    LOGI_DEVICES,
    LOGI_VID,
    hidpp_get_feature,
    hidpp_probe_device,
    hidpp_read_status,
    hidpp_xfer,
)
from .pulsar import (
    ARESON_DEVICES,
    ARESON_VID,
    ATK_PRODUCTS,
    ATK_VID,
    COMPX_DEVICES,
    COMPX_R1_RATES,
    COMPX_R1_RATES_INV,
    COMPX_RATES_ALL,
    COMPX_RATES_INV,
    COMPX_VID,
    PULSAR_PRO_DEVICES,
    PULSAR_PRO_SLEEP_MS,
    PULSAR_VID,
    PULSAR_XS1_DEVICES,
    XS1_LOD,
    XS1_LOD_INV,
    XS1_POLL_RATES,
    _compx_build,
    _compx_ctx,
    compx_checksum,
    compx_decode_dpi4,
    compx_decode_dpi6,
    compx_encode_dpi4,
    compx_encode_dpi6,
    compx_online,
    compx_read_flash,
    compx_read_status,
    compx_write_flash,
    compx_write_pair,
    compx_xfer,
    pulsar_pro_commit,
    pulsar_pro_read_status,
    pulsar_pro_xfer,
    pulsar_xs1_read_status,
    pulsar_xs1_xfer,
)
from .razer import (
    DEVICES,
    PIDS,
    POLL2_CODES,
    POLL2_HZ,
    POLL_CODES,
    POLL_HZ,
    RAZER_DEVICES,
    RAZER_VID,
    VID,
    _build,
    get_dpi,
    get_firmware,
    get_idle,
    get_lod,
    get_low,
    get_poll,
    get_serial,
    get_stages,
    razer_read_status,
    set_dpi,
    set_idle,
    set_lod_async,
    set_lod_preset,
    set_low,
    set_poll,
    set_stages,
    xfer,
)


def read_device_status(meta):
    drv = meta["driver"]
    fd = os.open(meta["hidraw"], os.O_RDWR)
    nfd = _open_notify(meta)
    try:
        if drv == "razer":
            return razer_read_status(fd, meta)
        if drv == "egg8k":
            return egg8k_read_status(fd, meta)
        if drv == "hidpp":
            info = hidpp_read_status(fd, meta)
            info["id"] = meta["id"]
            return info
        if drv in ("compx", "egg_we"):
            return compx_read_status(fd, meta, nfd)
        if drv == "pulsar_xs1":
            return pulsar_xs1_read_status(fd, meta)
        if drv == "pulsar_pro":
            return pulsar_pro_read_status(fd, meta)
        if drv == "glorious_sinowealth":
            blk = sino_read_block(fd, meta)
            parsed = sino_parse_block(blk)
            return {
                "id": meta["id"], "driver": drv, "device": meta["name"],
                "vid": f"0x{meta['vid']:04x}", "pid": f"0x{meta['pid']:04x}",
                "hidraw": meta["hidraw"], "link": meta["link"], "wireless": False,
                "dpi_min": 100, **parsed,
            }
        if drv == "glorious_core2":
            return core2_read_status(fd, meta)
        if drv == "glorious_classic":
            return classic_read_status(fd, meta)
        if drv == "glorious_pixart":
            return pixart_read_status(meta)
        raise ValueError(f"unsupported driver {drv!r}")
    finally:
        if nfd is not None:
            os.close(nfd)
        os.close(fd)
