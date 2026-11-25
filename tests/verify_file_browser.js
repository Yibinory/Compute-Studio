'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');

async function verify(url, root, project, task, server, attempt) {
  const browser = await chromium.launch({channel:'msedge',headless:true});
  const page = await browser.newPage({viewport:{width:1360,height:970},acceptDownloads:true});
  page.setDefaultTimeout(10000);
  const errors = [];
  page.on('pageerror',error=>errors.push(error.message));
  const dialog=page.locator('#dialog');
  const close=()=>page.locator('.dialog-top [data-action=close-dialog]').click();
  const folder=(pane,name)=>pane.locator('.file-folder-button').filter({hasText:name});
  const directory=(pane,name)=>pane.locator('.file-current-dir').getByText(name,{exact:true}).waitFor();
  async function verifyDatasetUsageColumns() {
    const rows=page.locator('.dataset-usage > .file-row');
    await rows.nth(2).waitFor();
    const columns=await rows.evaluateAll(items=>items.map(row=>[...row.children].map(el=>el.getBoundingClientRect())));
    assert.equal(columns.length,3);
    for(const [name,status,action] of columns){
      assert(Math.abs(status.left-columns[0][1].left)<1,'status badges share a column');
      assert(Math.abs(action.right-columns[0][2].right)<1,'detail buttons share a column');
      assert(name.right<=status.left&&status.right<=action.left,'long names and badges stay within their columns');
    }
  }
  try {
    await page.goto(url);
    await page.locator('[name=token]').fill(fs.readFileSync(path.join(root,'access-token.txt'),'utf8').trim());
    await page.locator('#login-form button').click();
    await page.locator('#main h1').waitFor();
    await page.goto(url+'#dataset/folders-fixture');
    await verifyDatasetUsageColumns();
    const data=page.locator('#dataset-files');
    await directory(data,'根目录');
    assert.equal(await data.locator('.file-folder-row').count(),5);
    assert.equal(await data.locator('.file-folder-row .file-entry-type').first().innerText(),'文件夹');
    assert.equal(await data.locator('.file-up').isDisabled(),true);
    await folder(data,'train').click();
    await folder(data,'images').click();
    await directory(data,'images');
    assert.equal(await data.locator('tbody tr').count(),100);
    await data.locator('[data-action=dataset-next]').click();
    await data.getByText('image_0100.txt',{exact:true}).waitFor();
    assert.equal(await data.locator('tbody tr').count(),100);
    await page.locator('#dataset-search input').fill('image_0209');
    await data.getByText('image_0209.txt',{exact:true}).waitFor();
    assert.equal(await data.locator('tbody tr').count(),1);
    await data.locator('[data-action=dataset-file-info]').click();
    assert.match(await dialog.locator('#dialog-body code').innerText(),/^[a-f0-9]{64}$/);
    await close();
    const [download]=await Promise.all([page.waitForEvent('download'),data.locator('a[download]').click()]);
    assert.equal(fs.readFileSync(await download.path(),'utf8'),'verified content');
    await data.getByRole('button',{name:'上一级'}).click();
    await directory(data,'train');
    await data.locator('.file-crumb').getByText('根目录',{exact:true}).click();
    await directory(data,'根目录');
    await page.locator('.dataset-inventory').screenshot({path:path.join(root,'dataset-folders.png')});

    await page.goto(url+'#project/'+project);
    await page.locator('[data-tab=files]').click();
    const code=page.locator('[data-file-scope=code]');
    await folder(code,'src').click();
    await directory(code,'src');
    await code.locator('[data-public-file-page]').check();
    assert.match(await code.locator('.public-file-delete-tools').innerText(),/已选 1 个文件/);
    await code.locator('[data-action=edit-file]').click();
    assert.equal(await dialog.locator('[name=text]').inputValue(),'print(42)');
    await close();
    await code.locator('[data-action=project-files-stats]').click();
    await dialog.getByText('1 个文件 · 9 B',{exact:true}).waitFor();
    await close();
    assert.equal(await page.locator('[data-file-scope]').count(),9);
    await page.locator('[data-action=project-file-tasks-next]').click();
    assert((await page.locator('[data-file-scope]').count())<9);

    await page.locator('[data-tab=replicas]').click();
    await page.locator('[data-action=replica-files][data-server="'+server+'"]').click();
    await folder(dialog,'src').click();
    await dialog.getByText('train.py',{exact:true}).waitFor();
    await dialog.locator('#replica-file-scope').selectOption('files');
    await folder(dialog,'src').click();
    await dialog.getByText('train.py',{exact:true}).waitFor();
    await close();

    await page.locator('[data-tab=tasks]').click();
    await page.locator('.node-title[data-id="'+task+'"]').click();
    const outputs=page.locator('#inspection-output-files');
    await folder(outputs,'outputs').click();
    await outputs.getByText('score.txt',{exact:true}).waitFor();
    await dialog.locator('[data-action=artifacts]').click();
    await dialog.locator('#artifact-attempt').selectOption(attempt);
    const artifacts=dialog.locator('#artifact-files');
    await folder(artifacts,'outputs').click();
    await artifacts.getByText('score.txt',{exact:true}).waitFor();
    await artifacts.locator('[data-action=file-browser-hash]').click();
    await artifacts.locator('code[title]').waitFor();
    await close();

    await page.locator('[data-tab=content]').click();
    await page.locator('[data-action=content-run-files][data-attempt="'+attempt+'"]').click();
    const run=dialog.locator('#content-run-file-list');
    await folder(run,'outputs').click();
    await run.getByText('score.txt',{exact:true}).waitFor();
    await close();

    await page.goto(url+'#transfer/upload/folders-upload');
    const transfers=page.locator('#transfer-reader-files');
    await folder(transfers,'train').click();
    await folder(transfers,'images').click();
    await transfers.locator('[data-action=transfer-file-detail]').first().click();
    await dialog.getByRole('heading',{name:'文件核验与操作记录'}).waitFor();
    await close();
    await page.locator('[data-action=transfer-detail-tab][data-tab=receipts]').click();
    await folder(transfers,'train').click();
    await folder(transfers,'images').click();
    await directory(transfers,'images');
    assert.equal(await transfers.locator('tbody tr').count(),100);
    await page.setViewportSize({width:390,height:844});
    await page.goto(url+'#dataset/folders-fixture');
    await verifyDatasetUsageColumns();
    await folder(data,'train').click();
    await directory(data,'train');
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    assert.deepEqual(errors,[]);
    console.log('PASS: folders, breadcrumbs, pagination, mobile layout, downloads, project edit and selection, task paging, replica scopes, run files, output and artifact hashes, transfer details and receipts.');
  } catch (error) {
    console.error('Browser errors:',errors);
    console.error((await page.locator('#main').innerText().catch(()=>'' )).slice(0,1000));
    throw error;
  } finally {
    await browser.close();
  }
}

verify(...process.argv.slice(2)).catch(error=>{console.error(error);process.exitCode=1;});
