// Render supplied Markdown only. This process never discovers private files.
import MarkdownIt from 'markdown-it';
import path from 'node:path';

let input = '';
for await (const chunk of process.stdin) input += chunk;
const {documents, publicPages} = JSON.parse(input);
const available = new Set(documents.map(doc => doc.path));
const md = new MarkdownIt({html: false, linkify: false, typographer: false});
md.disable('image');
const pages = documents.map(doc => {
  const tokens = md.parse(doc.text, {});
  const ids = new Map();
  for (let i = 0; i < tokens.length; i++) {
    const token = tokens[i];
    if (token.type === 'heading_open') {
      const base = (tokens[i + 1]?.content || 'section').toLowerCase()
        .replace(/[^\p{L}\p{N}\s_-]/gu, '').trim().replace(/\s+/g, '-');
      const count = ids.get(base) || 0;
      ids.set(base, count + 1);
      token.attrSet('id', base + (count ? `-${count}` : ''));
    }
    for (const child of token.children || []) {
      if (child.type !== 'link_open') continue;
      const href = child.attrGet('href');
      if (/^https?:\/\//i.test(href)) {
        child.attrSet('rel', 'noreferrer noopener');
        continue;
      }
      const [raw, fragment] = href.split('#');
      const target = path.posix.normalize(path.posix.join(path.posix.dirname(doc.path), decodeURIComponent(raw)));
      if (!raw) continue;
      if (available.has(target)) {
        const relative = path.posix.relative(path.posix.dirname(doc.path), target).replace(/\.md$/, '.html');
        child.attrSet('href', relative + (fragment ? `#${fragment}` : ''));
      } else if (target.startsWith('../haven/docs/') && publicPages.includes(target.slice(14))) {
        child.attrSet('href', 'https://ondrejkrpec.github.io/haven/haven/' + target.slice(14).replace(/\.adoc$/, '.html') + (fragment ? `#${fragment}` : ''));
      } else {
        // Undeclared source files remain visible as text; never copy attachments.
        child.attrs = child.attrs.filter(([key]) => key !== 'href');
        child.attrSet('title', 'Source file available only in the private repository');
      }
    }
  }
  return {path: doc.path.replace(/\.md$/, '.html'), html: md.renderer.render(tokens, md.options, {})};
});
process.stdout.write(JSON.stringify(pages));
