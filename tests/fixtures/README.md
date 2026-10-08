install-upstream.sh is an unexecuted regression fixture retrieved 2026-10-08 from
https://raw.githubusercontent.com/1Panel-dev/installer/v2/install.sh
SHA-256: 3faa744fd158283470b48b3a971dc98cc390c7f291d4287b170416ae862dd28f
It includes the new upstream Docker prompt structure that broke the old patch.
Upstream project and licensing: https://github.com/1Panel-dev/installer
Tests only read/transform this text and run bash -n; they never install or launch it.

install-release-v2.3.2.sh was extracted without execution from the official
v2.3.2 amd64 archive at
https://resource.fit2cloud.com/1panel/package/v2/stable/v2.3.2/release/1panel-v2.3.2-linux-amd64.tar.gz
