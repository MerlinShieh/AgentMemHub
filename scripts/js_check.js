// 提取 index.html 内嵌 <script> 并做语法检查
const fs = require("fs");
const html = fs.readFileSync("agentmemhub/web/static/index.html", "utf8");
const blocks = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
console.log("found", blocks.length, "inline script block(s)");
let fail = false;
blocks.forEach((code, i) => {
  try {
    new Function(code);   // 语法解析（不执行）
    console.log(`block ${i}: syntax OK (${code.length} chars)`);
  } catch (e) {
    console.log(`block ${i}: SYNTAX ERROR →`, e.message);
    fail = true;
  }
});
process.exit(fail ? 1 : 0);
