'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');

async function verify(url, dataDir) {
  const browser = await chromium.launch({channel: 'msedge', headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1280, height: 900}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(url);
    await page.locator('[name=token]').fill(fs.readFileSync(path.join(dataDir, 'access-token.txt'), 'utf8').trim());
    await page.locator('#login-form button').click();
    await page.locator('#main h1').waitFor();
    const snapshot = await page.evaluate(() => api('/api/state'));
    assert(snapshot.settings.demo, 'Use a seeded demo workspace');
    await page.route('**/api/state', route => route.fulfill({json: snapshot}));
    const task = snapshot.tasks.find(t => t.status === 'running');
    const poll = () => page.evaluate(() => load(true, true));
    await page.goto(url + '#project/' + task.project_id);
    const card = page.locator('[data-node-id="' + task.id + '"]');
    const title = card.locator('.node-title');
    await title.hover();
    const initialTitle = await title.elementHandle();
    const position = await page.locator('.graph-scroll').evaluate(el => [el.scrollTop, el.scrollLeft]);
    task.status = 'suspended';
    task.reason = 'interaction regression check';
    await poll();
    const keptTitle = await initialTitle.evaluate(el => el.isConnected);
    const unrelated = page.locator('.graph-node:not(.related)').first();
    if (await unrelated.count()) {
      await unrelated.evaluate(el => Promise.all(el.getAnimations().map(a => a.finished)));
    }
    const steadyOpacity = !(await unrelated.count()) || await unrelated.evaluate(el => getComputedStyle(el).opacity === '1');
    assert.match(await card.locator('.flow-status-text').innerText(), /暂停/);
    assert((await card.getAttribute('title')).includes(task.reason));
    assert.deepEqual(await page.locator('.graph-scroll').evaluate(el => [el.scrollTop, el.scrollLeft]), position);
    assert(await card.evaluate(el => el.classList.contains('related')));
    assert(await page.locator('.graph-edge.highlighted').count());
    task.status = 'success';
    task.synced_at = Date.now() / 1000;
    task.output_count = 4;
    snapshot.notes.push({id:'hover-test-note', task_id:task.id});
    await poll();
    assert.equal(await card.locator('.output-button').innerText(), '输出 4');
    assert.equal(await card.locator('.note-button').innerText(), 'Notes ' + snapshot.notes.filter(n => n.task_id === task.id).length);
    assert(await card.evaluate(el => el.classList.contains('complete')));
    assert(await page.locator('[data-map-id="' + task.id + '"]').evaluate(el => el.classList.contains('complete')));
    assert(await page.locator('.graph-edge.highlighted.done[data-source="' + task.id + '"]').count());
    const complete = await page.locator('.flow-node.complete').count();
    assert.equal(Number(await page.locator('.schedule-count.complete b').innerText()), complete);
    await title.click();
    await page.locator('#task-log').waitFor();
    await page.locator('.dialog-top [data-action=close-dialog]').click();

    // A structural change during a drag must be applied only after release.
    const scroll = page.locator('.graph-scroll');
    await scroll.evaluate(el => {el.style.maxHeight = '220px'; updateGraphMap(el); el.scrollTop = 0;});
    await title.hover();
    const point = await title.boundingBox();
    const x = point.x + point.width / 2, y = point.y + point.height / 2;
    const move = await scroll.evaluate(el => el.scrollTop > 60 ? 40 : -40);
    const start = await scroll.evaluate(el => el.scrollTop);
    await page.mouse.down();
    await page.mouse.move(x, y + move, {steps: 5});
    assert.equal(await scroll.getAttribute('data-mouse-pan'), 'dragging');
    assert.notEqual(await scroll.evaluate(el => el.scrollTop), start);
    snapshot.tasks.push({...task, id:'graph-new-node', seq:999, name:'New node', depends_on:[task.id]});
    await poll();
    assert.equal(await page.locator('[data-node-id="graph-new-node"]').count(), 0);
    await page.mouse.up();
    await page.locator('[data-node-id="graph-new-node"]').waitFor({state:'attached'});
    assert.equal(await page.locator('#dialog').evaluate(el => el.open), false);
    await page.goto(url + '#projects');
    const create = page.locator('#main [data-action=new-project]').first();
    await create.hover();
    await page.mouse.down();
    snapshot.projects[0].description += ' (updated)';
    await poll();
    await page.mouse.up();
    const opened = await page.locator('#dialog').evaluate(el => el.open);
    assert(keptTitle && steadyOpacity && opened, 'Hover and the first click must survive a background update');
    await page.locator('.dialog-top [data-action=close-dialog]').click();
    await create.focus();
    await page.keyboard.down('Space');
    snapshot.projects[0].description += ' (keyboard)';
    await poll();
    await page.keyboard.up('Space');
    await page.locator('#project-form').waitFor();
    await page.locator('.dialog-top [data-action=close-dialog]').click();

    for(const [section,action,form] of [
      ['overview','new-task','#task-form'],
      ['servers','new-server','#server-form'],
      ['datasets','new-dataset','#dataset-form']
    ]){
      await page.goto(url+'#'+section);
      const opener=page.locator('#main [data-action="'+action+'"]').first();
      await opener.hover();
      await page.mouse.down();
      snapshot.servers[0].name+='!';
      await poll();
      await page.mouse.up();
      await page.locator(form).waitFor();
      await page.locator('.dialog-top [data-action=close-dialog]').click();
    }

    // Hold back the old history request until after the user chooses a new filter.
    let releaseOldHistory;
    const oldHistory = new Promise(resolve => {releaseOldHistory = resolve;});
    await page.route('**/api/transfers?**', async route => {
      const status = new URL(route.request().url()).searchParams.get('status');
      if(status === 'all')await oldHistory;
      await route.fulfill({json:{entries:[],counts:{active:0,success:status === 'success'?7:0,stopped:0},total_count:0,offset:0,limit:30}});
    });
    const initialRequest = page.waitForRequest(req => req.url().includes('/api/transfers?'));
    await page.goto(url + '#uploads');
    await initialRequest;
    await page.locator('[data-action=transfer-filter][data-value=success]').click();
    await page.waitForFunction(() => transferHistory.data?.counts.success === 7);
    const staleResponse = page.waitForResponse(res => res.url().includes('status=all'));
    releaseOldHistory();
    await staleResponse;
    await page.waitForLoadState('networkidle');
    assert.equal(await page.locator('.transfer-list-toolbar button.active').getAttribute('data-value'),'success');
    assert.equal(await page.locator('.upload-summary b').nth(1).innerText(),'7');
    assert.deepEqual(errors, []);
    console.log('PASS: stable graph hover, live status/files/notes, drag during refresh, single-click details, mouse and keyboard activation, overview/server/dataset dialogs, concurrent transfer filters.');
  } finally {
    await browser.close();
  }
}
verify(process.argv[2], process.argv[3]).catch(error => {console.error(error); process.exitCode = 1;});
