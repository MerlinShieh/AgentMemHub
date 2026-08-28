// 截图验证「记忆引擎」板块渲染（8087 = E2E 临时库实例）
const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  await page.goto('http://127.0.0.1:8087/', { waitUntil: 'networkidle', timeout: 30000 });
  await page.waitForTimeout(1500); // 等记忆板块异步加载
  const panel = page.locator('#memosPanel');
  const visible = !(await panel.getAttribute('class')).includes('hidden');
  console.log('memosPanel 可见:', visible);
  console.log('状态文本:', await page.locator('#memosState').textContent());
  console.log('meta:', await page.locator('#memosMeta').textContent());
  const recent = await page.locator('#memosRecent > div').count();
  console.log('最近记忆条数:', recent);
  // 触发一次语义检索
  await page.fill('#memosKw', 'E2E');
  await page.keyboard.press('Enter');
  await page.waitForTimeout(1500);
  const hits = await page.locator('#memosHits > div').count();
  console.log('检索"E2E"命中渲染:', hits);
  await page.locator('#memosPanel').screenshot({ path: '_memos_panel.png' });
  console.log('截图: scripts/e2e/_memos_panel.png');
  await browser.close();
})();