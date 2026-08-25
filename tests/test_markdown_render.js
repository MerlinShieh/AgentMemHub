const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const src = fs.readFileSync(path.join(__dirname, "..", "static", "app.js"), "utf8");
const escapeStart = src.indexOf("function escapeHtml");
const escapeEnd = src.indexOf("\nfunction ", escapeStart + 1);
const mdStart = src.indexOf("function isMarkdownTableSeparator");
const mdEnd = src.indexOf("function dateTime");
assert.ok(escapeStart >= 0 && mdStart >= 0 && mdEnd > mdStart, "markdown helpers not found");

const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(`${src.slice(escapeStart, escapeEnd)}\n${src.slice(mdStart, mdEnd)}`, sandbox);

const sample = [
  "达令菁，搞定了！现在一共 **5 个窗口**。",
  "",
  "| # | PID | 内存 |",
  "|---|---|---|",
  "| 1 | 44568 | ~87 MB |",
  "| **5** | **38788** | **~74 MB** |",
  "",
  "看 [说明书](https://example.com/guide) 或 https://example.com/raw",
  "",
  "- [x] 已打开窗口",
  "- [ ] 还没做成快捷方式",
  "",
  "~~旧方案~~ 换成 Start-Process",
].join("\n");

const html = sandbox.renderRichText(sample, "");
assert.match(html, /<table class="md-table">/);
assert.match(html, /<strong>5 个窗口<\/strong>/);
assert.match(html, /<a class="md-link" href="https:\/\/example.com\/guide"/);
assert.match(html, /<li class="md-task">/);
assert.match(html, /<del>旧方案<\/del>/);
assert.doesNotMatch(html, /javascript:/);

const compact = sandbox.compactMarkdown(sample, "", 80);
assert.match(compact, /<strong>5 个窗口<\/strong>/);
assert.match(compact, /含表格/);
assert.doesNotMatch(compact, /<table/);
assert.doesNotMatch(compact, /href="https:\/\/example.com…"/);

const flat = sandbox.restorePipeTables(
  "现在有 3 个窗口： | PID | 内存 | 状态 | |---|---|---| | 44568 | ~90 MB | 原有 |"
);
assert.match(flat, /\n\| PID \| 内存 \| 状态\|/);
assert.equal((sandbox.renderRichText(flat).match(/<table/g) || []).length, 1);

console.log("PASS: markdown renderer tables/links/tasks/compact");
