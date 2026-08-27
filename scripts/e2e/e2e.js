// E2E：真实浏览器(Edge)实测 AgentMemHub 看板 —— 行点击开抽屉 / 编辑标题 / 删除会话
const { chromium } = require('playwright');

(async () => {
  const out = [];
  const log = (s) => { out.push(s); console.log(s); };
  let ok = 0, fail = 0;
  const check = (name, cond, extra = '') => {
    if (cond) { ok++; log(`  ✅ ${name}`); }
    else { fail++; log(`  ❌ ${name} ${extra}`); }
  };

  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const pageErrors = [];
  page.on('pageerror', e => pageErrors.push('pageerror: ' + e.message));
  page.on('console', m => { if (m.type() === 'error') pageErrors.push('console: ' + m.text()); });

  await page.goto('http://127.0.0.1:8087/', { waitUntil: 'networkidle', timeout: 30000 });
  await page.waitForSelector('#convTbody tr', { timeout: 15000 });
  log('== 1. 页面加载与列表渲染 ==');
  const rowCount = await page.locator('#convTbody tr').count();
  check('表格渲染出 3 行', rowCount === 3, `实际 ${rowCount}`);

  log('== 2. 点击行 → 打开详情抽屉 ==');
  await page.locator('#convTbody tr').first().locator('td').nth(2).click();
  await page.waitForSelector('#drawer:not(.hidden)', { timeout: 5000 });
  await page.waitForTimeout(600); // 等待事件加载
  const drawerVisible = await page.locator('#drawer').isVisible();
  check('抽屉可见', drawerVisible);
  const bodyText = await page.locator('#drawerBody').innerText();
  check('抽屉加载出事件内容(含 E2E)', bodyText.includes('E2E'), bodyText.slice(0, 40));
  check('抽屉渲染了思维链/工具类型', /思考|推理|工具/.test(bodyText));
  check('抽屉分页栏可见', await page.locator('#drawerPager').isVisible());
  const pageInfo = await page.locator('#drawerPageInfo').innerText();
  check('分页信息显示总条数', pageInfo.includes('4'), pageInfo);
  // 默认展示尾页（最新内容），页内升序：最早在上、最新在下
  const evTexts = await page.locator('#drawerBody .ev-bubble').allInnerTexts();
  const firstTxt = (evTexts[0] || '');
  const lastTxt = (evTexts[evTexts.length - 1] || '');
  check('尾页升序: 首条为最早用户提问', firstTxt.includes('的用户提问'), firstTxt.slice(0, 30));
  check('尾页升序: 末条为最新助手回复', lastTxt.includes('E2E 回复'), lastTxt.slice(0, 30));
  // 单页边界态：4 事件/页容 100 = 1 页 → 所有翻页按钮应 disabled（正确行为）
  const nav = await page.locator('#drawerPageNav').innerText();
  check('单页显示 1 / 1', nav.includes('1 / 1'), nav);
  check('单页: 首页 disabled', await page.locator('#drawerFirstPage').isDisabled());
  check('单页: 上一页 disabled', await page.locator('#drawerPrevPage').isDisabled());
  check('单页: 下一页 disabled', await page.locator('#drawerNextPage').isDisabled());
  check('单页: 末页 disabled', await page.locator('#drawerLastPage').isDisabled());
  // 默认折叠：推理/工具/补丁 details 未展开
  const closed = await page.locator('#drawerBody details:not([open])').count();
  check(`推理/工具等默认折叠(details 未展开>=2)`, closed >= 2, `closed=${closed}`);
  await page.locator('#drawerClose').click();
  await page.waitForTimeout(300);
  check('抽屉可关闭', !(await page.locator('#drawer').isVisible()));

  log('== 3. 编辑标题（PATCH 真实写库）==');
  const row = page.locator('#convTbody tr').first();
  await row.locator('.edit-btn').click();
  await page.waitForSelector('#modalRoot:not(.hidden)', { timeout: 5000 });
  await page.fill('#editTitle', 'E2E 已改名标题');
  await page.locator('#modalConfirm').click();
  await page.waitForTimeout(800); // PATCH + toast
  const titleText = await row.locator('td:nth-child(3) .font-medium').innerText();
  check('行标题已更新', titleText.includes('E2E 已改名标题'), titleText);
  check('编辑无页面错误', pageErrors.length === 0, pageErrors.join(' | '));

  log('== 4. 删除会话（DELETE 真实写库）==');
  const before = await page.locator('#convTbody tr').count();
  await row.locator('.del-btn').click();
  await page.waitForSelector('#modalRoot:not(.hidden)', { timeout: 5000 });
  await page.locator('#modalConfirm').click();
  await page.waitForTimeout(1000);
  const after = await page.locator('#convTbody tr').count();
  check(`删除后行数减少 (${before} → ${after})`, after === before - 1);
  check('整页无 JS 错误', pageErrors.length === 0, pageErrors.join(' | '));

  await browser.close();
  log(`\n结果: ${ok} 通过, ${fail} 失败`);
  process.exit(fail ? 1 : 0);
})().catch(e => { console.error('E2E 异常:', e); process.exit(2); });