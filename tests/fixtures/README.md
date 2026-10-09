Historical installer samples under historical-installers/ are named by SHA-256.
Identical bytes from different immutable commits share one file. index.json keeps
retrieval provenance and interface expectations for tests only; it contains no
application-version routing table. Tests verify each sample hash before use.

The sample 3faa744fd158283470b48b3a971dc98cc390c7f291d4287b170416ae862dd28f.sh
was also observed on 2026-10-08 in the installer v2 branch and extracted without
execution from the official v2.3.2 amd64 archive; both observations are retained
in the index. Its changed Docker prompt structure exercises the original patch
regression. Upstream project and licensing: https://github.com/1Panel-dev/installer

community-matrix.json is a synthetic test inventory, not a production whitelist.
Configuration, source and producer records used by consumer tests are constructed
as authenticated per-run fixtures with no production registry.

Historical shell tests use controlled stubs and temporary filesystem roots.
These samples never authorize executing downloaded applications or services and
do not constitute native runtime compatibility evidence.
