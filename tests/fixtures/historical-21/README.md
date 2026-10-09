# Historical 2.1 fixtures

`inputs.json` contains the three reviewed custom source entries from producer
`8e8ffd07a00c2660b559e999846b6bf6d82fc152`, tree
`117123fb751dba5130b0d6079fd5388641772bc8`, extracted from cached local commit
`eb56c91331eef8b9971cbf55723b229a24599f54` after verifying the existing tree binding.

The two `1pctl` files were extracted, without execution, from complete amd64
source streams checked on 2026-10-09 UTC. Official:
https://resource.fit2cloud.com/1panel/package/v2/stable/v2.1.12/release/1panel-v2.1.12-linux-amd64.tar.gz
Custom:
https://github.com/HandSonic/1Panel-Build-v2/releases/download/v2.1.12/1panel-v2.1.12-linux-amd64.tar.gz

- Official control SHA-256: `403b88e31bb753d6702b856b2f6c665edfae5a56a0a89bff103a6b1c476204c7`
- Custom control SHA-256: `4c31165ea5c4e45a81846aaff5a0d6cee4b095e0d333d0ef8b78d11ec80bd637`

Both preserve their real `ORIGINAL_VERSION=v2.1.12` and `PANEL_EDITION=cn`.
They intentionally differ: custom forwards additional arguments when updating
the username or password. Tests only read/hash these files and never run them.
The controls originate from https://github.com/1Panel-dev/installer.
