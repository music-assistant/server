import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { parse } from 'acorn';

for (const file of ['web.js', 'url-builder.js', 'index.html', 'builder-login.html']) {
    test(`${file} uses classic ES2015 syntax for the HTML5 profile`, async () => {
        const source = await readFile(new URL(`../../provider/static/web/${file}`, import.meta.url), 'utf8');
        const scripts = file.endsWith('.html')
            ? [...source.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map(match => match[1])
            : [source];
        for (const script of scripts) assert.doesNotThrow(() => parse(script, { ecmaVersion: 2015, sourceType: 'script' }));
    });
}

test('Sendspin is an isolated classic bundle without optional chaining or nullish syntax', async () => {
    const source = await readFile(new URL('../../provider/static/web/sendspin-js/index.js', import.meta.url), 'utf8');
    const tree = parse(source, { ecmaVersion:2020, sourceType:'script' });
    function visit(node) {
        if (!node || typeof node !== 'object') return;
        assert.notEqual(node.type, 'ChainExpression');
        if (node.type === 'LogicalExpression') assert.notEqual(node.operator, '??');
        for (const value of Object.values(node)) {
            if (Array.isArray(value)) value.forEach(visit);
            else if (value && typeof value === 'object') visit(value);
        }
    }
    visit(tree);
});
