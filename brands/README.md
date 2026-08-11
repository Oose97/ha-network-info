# Brand assets

Home Assistant does not read integration icons from the integration itself —
they are served from the central [home-assistant/brands](https://github.com/home-assistant/brands)
repository, which both the frontend and HACS query per domain.

This folder stages what that repository expects for a custom integration,
ready to copy into a `custom_integrations/network_info/` PR there:

| File | Size |
|---|---|
| `icon.png` | 256×256 |
| `icon@2x.png` | 512×512 |

Until such a PR is merged, Home Assistant shows the generic puzzle-piece for
this integration; nothing in this folder changes runtime behavior.
