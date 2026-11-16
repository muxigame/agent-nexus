# Third-party notices

agent-nexus is licensed under the Apache License 2.0 (see `LICENSE`). This file
lists third-party material that is included in, derived from, or optionally
downloaded by this repository. Python and Rust dependencies installed from
their package registries keep their own licenses; the lock files
(`uv.lock`, `builtins/*/uv.lock`, `desktop/src-tauri/Cargo.lock`) list them
exactly.

## Optional vision models: OmniParser and ultralytics (AGPL-3.0, not distributed)

The `vision` tool of the device plugin can load OmniParser v2 (an icon detector
derived from YOLOv8 plus a Florence-2 caption model).

- OmniParser repository code: MIT. The icon-detection weights are a YOLOv8
  derivative; `ultralytics` is licensed under **AGPL-3.0**.
- These packages are only in the optional `vision` extra of
  `builtins/device/pyproject.toml`. Neither the Python packages nor the
  model weights are committed to this repository or bundled into any release
  artifact (installer, runtime ZIP, wheel or Debian package). They are
  downloaded on the user's machine only when the user installs the extra and
  loads the model.
- Users who enable the extra are responsible for complying with the AGPL-3.0
  and the model licenses for their use.

## Business plugins (not part of this repository)

The WeCom, payment, jubian-go, ONES, Unity and Chrome DevTools plugins live in
separate private repositories with their own licenses and notices (for
example, the upstream licenses of wecom-reader, unity-mcp and
chrome-devtools-mcp). A local build bundles them only when such a checkout is
present under `builtins/`; public builds never include them.

## ripgrep (MIT or Unlicense)

Windows runtime packages ship `tools/rg.exe`, an unmodified ripgrep release
binary by Andrew Gallant (https://github.com/BurntSushi/ripgrep), used by the
device plugin's `file_grep`. ripgrep is dual-licensed under the MIT license and
the Unlicense; Agent Nexus uses it under the MIT license.

## Lucide icons (vendored subset)

`agent_nexus/static/ui/icons.js` contains the SVG markup of a small subset of the Lucide icon set,
extracted from `lucide-react` v1.47.0, so the web consoles need no CDN at runtime.

- Project: https://lucide.dev
- License: ISC; the icons derived from Feather are MIT (copy: `agent_nexus/static/ui/LICENSE-lucide.txt`)

## QR Code generator (port)

`agent_nexus/static/ui/qr.js` is a compact JavaScript port of the encoding algorithm of Project Nayuki's
"QR Code generator library" (used to show invite links as QR codes without a CDN). It was checked
module-for-module against the npm package `qrcode` 1.5.4 (MIT); `tests/fixtures/qr_golden.json` holds
matrices generated with that package.

- Project: https://www.nayuki.io/page/qr-code-generator-library
- License: MIT

  Copyright (c) Project Nayuki. (MIT License)

  Permission is hereby granted, free of charge, to any person obtaining a copy of
  this software and associated documentation files (the "Software"), to deal in
  the Software without restriction, including without limitation the rights to
  use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
  the Software, and to permit persons to whom the Software is furnished to do so,
  subject to the following conditions:
  - The above copyright notice and this permission notice shall be included in
    all copies or substantial portions of the Software.
  - The Software is provided "as is", without warranty of any kind, express or
    implied, including but not limited to the warranties of merchantability,
    fitness for a particular purpose and noninfringement. In no event shall the
    authors or copyright holders be liable for any claim, damages or other
    liability, whether in an action of contract, tort or otherwise, arising from,
    out of or in connection with the Software or the use or other dealings in the
    Software.
