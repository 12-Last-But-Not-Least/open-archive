import {test, expect} from '@playwright/test';

test('two researchers create a lab, join, publish and update its public page', async ({browser}, info) => {
  const origin = 'http://127.0.0.1:8001';
  const contexts = [];
  const errors = [];
  const unexpected = [];
  async function newUser() {
    const context = await browser.newContext({...info.project.use, baseURL: origin});
    contexts.push(context);
    context.on('page', page => page.on('pageerror', error => errors.push(error.message)));
    // The app's API and HTML are never mocked. Only public upstream PDS reads
    // cross this boundary; they reflect the records written through the real API.
    await context.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (url.origin === origin) return route.continue();
      if (url.origin === 'https://plc.directory') {
        return route.fulfill({json: {id: url.pathname.slice(1), service: [{
          type: 'AtprotoPersonalDataServer', serviceEndpoint: 'https://pds.example.test',
        }]}});
      }
      if (url.origin === 'https://pds.example.test' && url.pathname === '/xrpc/com.atproto.repo.listRecords') {
        const response = await context.request.get('/__test__/records', {params: {repo: url.searchParams.get('repo')}});
        return route.fulfill({json: await response.json()});
      }
      unexpected.push(url.origin + url.pathname);
      return route.abort();
    });
    return context.newPage();
  }
  async function signIn(page, handle) {
    await page.getByLabel('ATProto handle').fill(handle);
    await page.getByRole('button', {name: 'Sign in', exact: true}).click();
    await expect(page.locator('#identity')).toHaveText('@' + handle);
  }
  async function profile(page, name) {
    await page.locator('#account summary').filter({hasText: 'Create or edit your researcher profile'}).click();
    await page.getByLabel('Your name', {exact: true}).fill(name);
    await page.locator('#profile-institution').fill('Example University');
    await page.getByRole('button', {name: 'Save profile', exact: true}).click();
    await expect(page.locator('#status')).toHaveText('Profile saved.');
  }
  try {
    const owner = await newUser();
    await owner.goto('/');
    await owner.getByRole('link', {name: 'Set up your profile and lab'}).click();
    await signIn(owner, 'bhackel.bsky.social');
    await profile(owner, 'Lab Owner');
    await owner.getByText('Add a lab', {exact: true}).click();
    // Valid single-word names must wrap in account, invitation, and public views.
    const labName = 'Bioinformatics'.repeat(10) + ' ' + info.project.name;
    await owner.getByLabel('Lab name', {exact: true}).fill(labName);
    await owner.locator('#lab-institution').fill('Example University');
    await owner.getByLabel('Description', {exact: true}).fill('Research shared by our members.');
    await owner.getByRole('button', {name: 'Add lab', exact: true}).click();
    const labLink = owner.locator('#my-labs').getByRole('link', {name: labName});
    await expect(labLink).toBeVisible();
    await expect(owner.locator('html')).toHaveJSProperty('scrollWidth', owner.viewportSize().width);
    const labURL = await labLink.getAttribute('href');
    const labSection = owner.locator('#my-labs section').filter({has: owner.getByRole('link', {name: labName, exact: true})});
    await labSection.getByRole('button', {name: 'Create invitation link', exact: true}).click();
    const invitation = await labSection.getByLabel('Lab invitation link').inputValue();

    const member = await newUser();
    await member.goto(invitation);
    const dialog = member.getByRole('dialog');
    await expect(dialog).toContainText(labName);
    expect(await dialog.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
    await member.getByRole('button', {name: 'Continue', exact: true}).click();
    await expect(member.getByLabel('ATProto handle')).toBeFocused();
    await signIn(member, 'member.bsky.social');
    await profile(member, 'Lab Member');
    await member.getByRole('button', {name: 'Join lab', exact: true}).click();
    await expect(member.locator('#status')).toHaveText('Joined ' + labName + '.');
    await expect(member.locator('#lab-invitation')).toBeHidden();
    await member.getByRole('button', {name: 'Find papers', exact: true}).click();
    await expect(member.locator('#suggestions')).toContainText('Shared lab research');
    await member.getByRole('button', {name: 'Add selected papers', exact: true}).click();
    await expect(member.locator('#own-papers')).toContainText('Shared lab research');
    const profileURL = await member.locator('#profile a').getAttribute('href');

    const visitor = await newUser();
    await visitor.goto('/labs/');
    const listedLab = visitor.getByRole('link', {name: labName, exact: true});
    await expect(listedLab).toBeVisible();
    await expect(visitor.locator('html')).toHaveJSProperty('scrollWidth', visitor.viewportSize().width);
    await listedLab.click();
    await expect(visitor.locator('#lab-members')).toContainText('Lab Member');
    await expect(visitor.locator('#lab-members')).toContainText('Lab Owner');
    await expect(visitor.locator('#publication-list')).toContainText('Shared lab research');
    await expect(visitor.locator('#lab-about')).toHaveText('Research shared by our members.');
    await expect(visitor.locator('html')).toHaveJSProperty('scrollWidth', visitor.viewportSize().width);
    await visitor.reload();
    await expect(visitor.locator('#publication-list')).toContainText('Shared lab research');
    await visitor.goto(profileURL);
    await expect(visitor.locator('#publication-list')).toContainText('Shared lab research');
    await expect(visitor.locator('#affiliations')).toContainText(labName);

    await member.reload();
    await expect(member.locator('#my-labs')).toContainText(labName);
    member.once('dialog', dialog => dialog.accept());
    await member.getByRole('button', {name: 'Leave lab', exact: true}).last().click();
    await expect(member.locator('#my-labs')).not.toContainText(labName);
    await visitor.goto(labURL);
    await expect(visitor.locator('#lab-members')).not.toContainText('Lab Member');
    await expect(visitor.locator('#publication-list')).toContainText('No publications added yet.');
    await visitor.goto(profileURL);
    await expect(visitor.locator('#publication-list')).toContainText('Shared lab research');
    // Clean up the simulated PDS for the next viewport's independent run.
    member.once('dialog', dialog => dialog.accept());
    await member.locator('#own-papers').getByRole('button', {name: 'Remove', exact: true}).click();
    await expect(member.locator('#own-papers')).not.toContainText('Shared lab research');
    expect(errors).toEqual([]);
    expect(unexpected).toEqual([]);
  } finally {
    await Promise.all(contexts.map(context => context.close()));
  }
});
