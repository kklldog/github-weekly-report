// Real publication, with durable submission receipts and no automatic duplicate submission.
import { chromium } from 'playwright-core';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import crypto from 'node:crypto';

const folder = process.env.WECHAT_POST_DIR;
if (!folder) throw new Error('WECHAT_POST_DIR required');
const post = JSON.parse(fs.readFileSync(path.join(folder, 'post.json'), 'utf8'));
const title = post.editorial.title;
const caption = `${post.editorial.content}\n\n作者：${post.author}`;
const images = fs.readdirSync(path.join(folder, 'images')).filter(f => f.endsWith('.png')).sort()
  .map(f => path.join(folder, 'images', f));
if (!title || title.length > 20 || caption.length > 1000 || images.length < 3 || images.length > 9)
  throw new Error('Invalid title/caption/image limits');
const stateFile = path.join(folder, 'wechat-state.json');
const resultFile = process.env.WECHAT_RESULT_FILE || path.join(folder, 'wechat-result.json');
const journalFile = path.join(folder, 'browser-journal.json');
let journal = fs.existsSync(journalFile) ? JSON.parse(fs.readFileSync(journalFile, 'utf8')) : {};
const write = (file, data) => {
  fs.writeFileSync(file + '.tmp', JSON.stringify(data, null, 2));
  fs.renameSync(file + '.tmp', file);
};
const updateJournal = data => { journal = {...journal, ...data}; write(journalFile, journal); };
const state = data => write(stateFile, {updated_at: new Date().toISOString(), ...data});
const sleep = ms => new Promise(r => setTimeout(r, ms));
let context, page;
let lastQr = '';
async function qr(status) {
  const img = page.locator('img.js_qrcode:visible, img[src*="scanloginqrcode"]:visible, img.login__type__container__scan__qrcode:visible').first();
  if (!await img.count()) return;
  if (!await img.evaluate(e=>e.complete && e.naturalWidth>0)) return;
  const src = await img.getAttribute('src');
  const id = crypto.createHash('sha256').update(status + ':' + src).digest('hex').slice(0, 16);
  if (id !== lastQr) {
    const qrPath = path.join(folder, `${status}-${id}.png`);
    await page.screenshot({path: qrPath});
    state({status, qr_path: qrPath, qr_id: id});
    lastQr = id;
  }
}
async function home() {
  const token = new URL(page.url()).searchParams.get('token');
  if (!token) throw new Error('WeChat login token missing');
  await page.goto(`https://mp.weixin.qq.com/cgi-bin/home?t=home/index&token=${token}&lang=zh_CN`, {waitUntil:'domcontentloaded'});
  await page.locator('.weui-desktop-mass-appmsg__title').first().waitFor({timeout:30000}).catch(()=>{});
}
async function receipt() {
  if (!journal.final_clicked && !journal.public_url) return null;
  return page.locator('a.weui-desktop-mass-appmsg__title').evaluateAll((es, expected) => {
    for (const a of es) {
      if(a.textContent.trim() !== expected.title) continue;
      const card = a.closest('.publish_hover_content, .weui-desktop-mass__content');
      if(!card) continue;
      const timestampLink=card.querySelector('a[href*="send_time="]');
      const sent=timestampLink?Number(new URL(timestampLink.href).searchParams.get('send_time')):0;
      if(expected.url ? a.href!==expected.url : !sent || sent < expected.after-60) continue;
      return {text:card.innerText, public_url:a.href, send_time:sent};
    }
    return null;
  }, {title,url:journal.public_url,after:Date.parse(journal.submitted_at||'')/1000});
}
async function reconcile() {
  await home();
  const r = await receipt();
  if (r && /(^|\n)已发表(\n|$)/.test(r.text)) {
    const evidence = path.join(folder, 'published-evidence.png');
    await page.screenshot({path:evidence});
    const result = {status:'published', platform_status:'已发表', public_url:r.public_url,
      draft_id:journal.draft_id, verified_at:new Date().toISOString(), evidence};
    updateJournal(result); write(resultFile, result); state(result);
    return 'published';
  }
  if (r && /审核中/.test(r.text)) { state({status:'reviewing', public_url:r.public_url}); return 'reviewing'; }
  if (r && /失败|未通过|无法查看/.test(r.text)) throw new Error('微信后台发表记录异常：' + r.text.slice(0,200));
  return null;
}
async function waitReceipt() {
  for (let i=0; i<60; i++) {
    const stage = await reconcile();
    if (stage === 'published') return;
    await sleep(30000);
  }
  const r = await receipt();
  const result = {status:r && /审核中/.test(r.text)?'reviewing':'publish_unknown',
    error:'已提交发表，后台结果尚待核实；禁止重复提交', public_url:r?.public_url};
  write(resultFile,result); state(result);
}
try {
  context = await chromium.launchPersistentContext(process.env.WECHAT_BROWSER_PROFILE_DIR || path.join(os.homedir(),'.config/wechat-github-weekly'), {
    executablePath:process.env.WECHAT_BROWSER_CHROME_PATH || '/usr/bin/google-chrome',
    headless:process.env.WECHAT_HEADLESS !== '0', viewport:{width:1440,height:1100},
    args:['--disable-blink-features=AutomationControlled'],
  });
  page = context.pages()[0] || await context.newPage();
  await page.goto('https://mp.weixin.qq.com/', {waitUntil:'domcontentloaded'});
  const loginDeadline = Date.now() + Number(process.env.WECHAT_SCAN_TIMEOUT_SECONDS || 86400)*1000;
  while (!new URL(page.url()).searchParams.get('token')) {
    if (Date.now()>loginDeadline) throw new Error('微信登录扫码等待超时');
    await qr('waiting_login');
    const refresh = page.getByText('刷新二维码',{exact:true});
    if (await refresh.isVisible().catch(()=>false)) await refresh.click();
    await sleep(3000);
  }
  const existing = await reconcile();
  if (existing === 'published') process.exitCode = 0;
  else if (journal.final_clicked || existing === 'reviewing' || process.argv.includes('--reconcile') || process.env.WECHAT_RECONCILE_ONLY === '1') {
    await waitReceipt();
  } else {
    state({status:'editing'});
    if (journal.draft_id) {
      const token = new URL(page.url()).searchParams.get('token');
      await page.goto(`https://mp.weixin.qq.com/cgi-bin/appmsg?t=media/appmsg_edit&action=edit&type=77&appmsgid=${journal.draft_id}&token=${token}&lang=zh_CN`);
    } else {
      const popup = context.waitForEvent('page');
      await page.getByText('贴图',{exact:true}).click();
      page = await popup;
      await page.waitForLoadState('domcontentloaded');
    }
    await page.locator('[name=title] .ProseMirror').waitFor();
    await page.locator('[name=title] .ProseMirror').fill(title);
    // Editor paste preserves line breaks and lets WeChat resolve topic markup.
    await page.locator('.share-text__input .ProseMirror').evaluate((el, text) => {
      el.focus();
      const range=document.createRange();range.selectNodeContents(el);
      const selection=getSelection();selection.removeAllRanges();selection.addRange(range);
      document.execCommand('delete');
      const data=new DataTransfer();data.setData('text/plain',text);
      el.dispatchEvent(new ClipboardEvent('paste',{clipboardData:data,bubbles:true,cancelable:true}));
    }, caption);
    if (!journal.draft_id) {
      await page.locator('input[type=file]:not([name])').setInputFiles(images);
      await page.getByText('默认首图为封面',{exact:true}).waitFor({timeout:180000});
    }
    await page.waitForFunction(expected=>{
      const thumbs=Array.from(document.querySelectorAll('.image-selector__bottom-list-item'));
      return thumbs.length===expected && thumbs.every(e=>/https:\/\/mmbiz\./.test(e.style.backgroundImage));
    },images.length,{timeout:180000});
    const actual = await page.locator('.share-text__input .ProseMirror').innerText();
    const normalize = s => s.replace(/\s+/g,'');
    if (normalize(actual)!==normalize(caption)) throw new Error('微信编辑器正文回读不一致，停止发表');
    if (await page.locator('[name=title] .ProseMirror').innerText() !== title) throw new Error('标题回读不一致');
    await page.locator('.js_claim_source_desc').click();
    await page.getByText('内容由AI生成',{exact:true}).filter({visible:true}).last().click();
    await page.locator('button.weui-desktop-btn_primary:visible').filter({hasText:/^确认$/}).click();
    await page.getByText('保存为草稿',{exact:true}).click();
    await page.waitForFunction(()=>new URL(location.href).searchParams.has('appmsgid'),{},{timeout:30000});
    updateJournal({draft_id:new URL(page.url()).searchParams.get('appmsgid'), status:'draft_saved'});
    await page.screenshot({path:path.join(folder,'before-publish.png')});
    state({status:'draft_saved'});
    if (process.argv.includes('--draft-only')) {
      fs.writeFileSync(path.join(folder,'editor-diagnostics.html'),await page.content());
      write(resultFile,{status:'draft_saved',draft_id:journal.draft_id});
    } else {
      await page.locator('button.mass_send').click();
      const submit = page.locator('button.weui-desktop-btn_primary:visible').filter({hasText:/^发表$/});
      await submit.waitFor({timeout:30000});
      updateJournal({final_clicked:true,status:'submitted',submitted_at:new Date().toISOString()});
      await submit.click();
      const deadline=Date.now()+Number(process.env.WECHAT_SCAN_TIMEOUT_SECONDS||86400)*1000;
      while (!page.url().includes('/cgi-bin/home')) {
        if (Date.now()>deadline) throw new Error('最终发表验证等待超时；请核实后台，禁止重复提交');
        if (await page.locator('img.js_qrcode:visible').count()) await qr('waiting_publish_scan');
        await sleep(3000);
      }
      await waitReceipt();
    }
  }
} catch (error) {
  console.error(error.stack);
  const result={status:journal.final_clicked?'publish_unknown':'failed', error:String(error.message)};
  if (page && !page.isClosed()) await page.screenshot({path:path.join(folder,'publish-error.png')}).catch(()=>{});
  write(resultFile,result);state(result);process.exitCode=1;
} finally {
  if(context) await context.close();
}
