import { build } from 'esbuild';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { patchCrx } from './crx-compat.mjs';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const require = createRequire(import.meta.url);
const upstream = dirname(require.resolve('playwright-crx/package.json'));
const output = resolve(root, 'extension/vendor');
const pkg = JSON.parse(await readFile(resolve(upstream, 'package.json'), 'utf8'));
await mkdir(output, { recursive: true });
await build({
  entryPoints: [resolve(root, 'extension/crx-driver.mjs')],
  outfile: resolve(output, 'crx-driver.js'), bundle: true, format: 'iife',
  platform: 'browser', target: 'chrome125', minify: true, legalComments: 'external',
  // Upstream ships unused Node-only BrowserType launch/BiDi branches. Leave their
  // require calls unresolved: this extension exclusively uses crx.start/attach.
  external: ['../playwright', './bidiOverCdp'],
  plugins: [{ name: 'crx-oopif-compat', setup(builder) {
    builder.onLoad({ filter: /playwright-crx\/lib\/index-[^/]+\.mjs$/ }, async ({ path }) => ({
      contents: patchCrx(await readFile(path, 'utf8'), pkg.version), loader: 'js',
    }));
  } }],
});
const notices = await Promise.all(['LICENSE', 'NOTICE', 'playwright/LICENSE', 'playwright/NOTICE'].map(async file =>
  `\n--- playwright-crx ${pkg.version}: ${file} ---\n${await readFile(resolve(upstream, file), 'utf8')}`));
await writeFile(resolve(output, 'THIRD_PARTY_NOTICES.txt'),
  'IDly modifications: pinned compatibility and input/popup integration patch in scripts/crx-compat.mjs.\n' + notices.join('\n'));
console.log(`Built extension/vendor/crx-driver.js (playwright-crx ${pkg.version})`);
