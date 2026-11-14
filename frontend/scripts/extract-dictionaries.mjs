import fs from 'node:fs';
import path from 'node:path';
import ts from 'typescript';
const ru = {}, kk = {};
const add = (key, value) => { if (key && value) { ru[key] = key; kk[key] = value; } };
function walkDirectory(directory) {
  for (const entry of fs.readdirSync(directory, {withFileTypes:true})) {
    const file = path.join(directory, entry.name);
    if (entry.isDirectory()) { if(entry.name !== 'dictionaries') walkDirectory(file); continue; }
    if (!/\.tsx?$/.test(file) || file.includes('.test.')) continue;
    const ast = ts.createSourceFile(file, fs.readFileSync(file,'utf8'), ts.ScriptTarget.Latest, true, file.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS);
    const stringValue = node => node && (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) ? node.text : null;
    function visit(node) {
      if (ts.isCallExpression(node) && node.expression.getText(ast) === 't' && node.arguments.length >= 2) add(stringValue(node.arguments[0]), stringValue(node.arguments[1]));
      if (ts.isObjectLiteralExpression(node)) {
        const values = {};
        for (const item of node.properties) if(ts.isPropertyAssignment(item)) values[item.name.getText(ast).replace(/^['"]|['"]$/g,'')] = stringValue(item.initializer);
        if(values.ru && values.kk) add(values.ru,values.kk);
        if(file.endsWith('locales.ts')) for(const [key,value] of Object.entries(values)) if(value) add(key,value);
      }
      if(ts.isArrayLiteralExpression(node) && node.elements.length >= 2) {
        const a=stringValue(node.elements[0]), b=stringValue(node.elements[1]);
        if(a && b && /[А-Яа-я]/.test(a) && /[А-Яа-яӘәҒғҚқҢңӨөҰұҮүҺһІі]/.test(b)) add(a,b);
      }
      ts.forEachChild(node,visit);
    }
    visit(ast);
  }
}
walkDirectory('src');
for(const [language,dictionary] of [['ru',ru],['kk',kk]]) fs.writeFileSync(`src/lib/dictionaries/${language}.json`,JSON.stringify(Object.fromEntries(Object.entries(dictionary).sort(([a],[b])=>a.localeCompare(b))),null,2)+'\n');
console.log(`Extracted ${Object.keys(ru).length} RU/KK dictionary entries.`);
