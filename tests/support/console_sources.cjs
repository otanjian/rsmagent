const fs = require('node:fs');
const path = require('node:path');
const web = path.resolve(__dirname, '../../channel/web');
function markup(relative = 'chat.html') {
    return fs.readFileSync(path.join(web, relative), 'utf8').replace(/<!--#include\s+([^\s>]+)\s*-->/g,
        (_, included) => markup(included));
}
function modules() {
    return [...markup().matchAll(/<script[^>]+src="\/assets\/(js\/[^"?]+)[^"]*"/g)]
        .map(match => ({ name: match[1], source: fs.readFileSync(path.join(web, 'static', match[1]), 'utf8') }));
}
function script(relative) {
    const loaded = modules().find(item => item.name === relative);
    if (!loaded) throw new Error('Requested script is not loaded: ' + relative);
    return loaded.source;
}
module.exports = { markup, modules, script };
