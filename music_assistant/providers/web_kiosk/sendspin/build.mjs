import { build } from 'esbuild';
import { createHash } from 'node:crypto';
import { readdir, readFile, mkdir, rm, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.dirname(fileURLToPath(import.meta.url));
const destination = path.resolve(root, '../static/web/sendspin-js');
const packages = ['@sendspin/sendspin-js', '@noble/curves', '@noble/hashes', '@noble/ciphers'];
const files = new Map();
const versions = {};
const licenses = [];

for (const name of packages) {
    const directory = path.join(root, 'node_modules', name);
    const info = JSON.parse(await readFile(path.join(directory, 'package.json'), 'utf8'));
    const license = (await readFile(path.join(directory, 'LICENSE'), 'utf8')).trimEnd();
    if (license.includes('*/')) throw new Error(`License cannot be embedded in a JS comment: ${name}`);
    versions[name] = info.version;
    licenses.push(`${name}@${info.version}\n${license}`);
}

const result = await build({
    absWorkingDir: root,
    entryPoints: ['index.js'],
    outfile: 'index.js',
    bundle: true,
    platform: 'browser',
    format: 'iife',
    globalName: 'WebKioskSendspin',
    target: ['chrome67', 'safari15', 'firefox68'],
    minify: true,
    legalComments: 'none',
    banner: { js: `/*!\nGenerated Sendspin SDK: bundled and minified for Web Kiosk; software Opus omitted.\n\n${licenses.join('\n\n')}\n*/\nif(typeof globalThis==='undefined'&&typeof window!=='undefined')window.globalThis=window;` },
    write: false,
    metafile: true,
    plugins: [{
        name: 'unsupported-opus-fallback',
        setup(builder) {
            // Keep the existing WebCodecs/FLAC/PCM policy without unresolved browser imports.
            builder.onResolve({ filter: /^opus-encdec\/(dist\/libopus-decoder|src\/oggOpusDecoder)\.js$/ },
                args => ({ path: args.path, namespace: 'unsupported-opus' }));
            builder.onLoad({ filter: /.*/, namespace: 'unsupported-opus' }, () => ({
                contents: 'throw new Error("Web Kiosk requires WebCodecs for Opus; use FLAC or PCM instead.");',
                loader: 'js',
            }));
        },
    }],
});

for (const output of Object.values(result.metafile.outputs)) {
    if (output.imports.length) throw new Error('Sendspin bundle has unresolved imports');
}
files.set('index.js', result.outputFiles[0].contents);
const esbuild = JSON.parse(await readFile(path.join(root, 'node_modules/esbuild/package.json'), 'utf8'));
files.set('VERSION.txt', `@sendspin/sendspin-js@${versions['@sendspin/sendspin-js']}\n`);
files.set('BUILD.json', JSON.stringify({
    packages: versions,
    source: 'sendspin/index.js',
    build: 'sendspin/build.mjs',
    esbuild: esbuild.version,
    format: 'iife',
    target: ['chrome67', 'safari15', 'firefox68'],
    sha256: createHash('sha256').update(files.get('index.js')).digest('hex'),
    opusFallback: false,
    licensePlacement: 'bundle-header',
}, null, 2) + '\n');

async function existingFiles(directory, prefix = '') {
    const found = [];
    for (const entry of await readdir(directory, { withFileTypes: true })) {
        const relative = path.posix.join(prefix, entry.name);
        if (entry.isDirectory()) found.push(...await existingFiles(path.join(directory, entry.name), relative));
        else found.push(relative);
    }
    return found.sort();
}

if (process.argv.includes('--check')) {
    const existing = await existingFiles(destination);
    if (JSON.stringify(existing) !== JSON.stringify([...files.keys()].sort())) {
        throw new Error('Sendspin asset list differs; run npm run build');
    }
    for (const [name, content] of files) {
        if (!Buffer.from(content).equals(await readFile(path.join(destination, name)))) {
            throw new Error(`Sendspin asset differs: ${name}; run npm run build`);
        }
    }
    console.log('Sendspin assets match the locked npm build.');
} else {
    await rm(destination, { recursive: true, force: true });
    for (const [name, content] of files) {
        const target = path.join(destination, name);
        await mkdir(path.dirname(target), { recursive: true });
        await writeFile(target, content);
    }
    console.log(`Built ${files.size} Sendspin assets (${files.get('index.js').byteLength} JS bytes).`);
}
