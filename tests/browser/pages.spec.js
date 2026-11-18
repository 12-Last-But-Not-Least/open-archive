import {test, expect, capture, accountReady, paper} from './fixtures.js';

test('home and account help', async ({page, mock}, info) => {
  await page.goto('/');
  await expect(page.getByRole('navigation')).toBeVisible();
  await capture(page, info, 'home');
  // Keep before/after captures compatible with the old homepage help box.
  const oldHelp = page.getByText('Make an Account', {exact: true});
  if (await oldHelp.count()) await oldHelp.click();
  await capture(page, info, 'home-help');
  await page.goto('/account/');
  await accountReady(page);
  const help = page.locator('.sign-in-help');
  if (await help.count()) {
    await expect(help).toBeVisible();
    await expect(help).not.toHaveAttribute('open');
    await help.locator('summary').click();
    await expect(help).toContainText('Create one on Bluesky');
  } else {
    await page.getByText('Make an Account', {exact: true}).click();
  }
  await capture(page, info, 'account-sign-in-help');
  mock.signedIn = true;
  await page.reload();
  await accountReady(page);
  if (await help.count()) await expect(help).toBeHidden();
});

for (const kind of ['researchers', 'labs']) {
  for (const state of ['populated', 'empty', 'error']) {
    test(`${kind} directory ${state}`, async ({page, mock}, info) => {
      mock.empty = state === 'empty'; mock.error = state === 'error';
      await page.goto(`/${kind}/`);
      if (state === 'populated') {
        await expect(page.locator('#directory-list .directory-item')).toHaveCount(1);
        await expect(page.locator('#directory-list')).toContainText('Example University');
      }
      else await expect(page.locator('#directory-status')).toContainText(state === 'empty' ? `No ${kind}` : 'Could not refresh');
      await capture(page, info, `${kind}-${state}`);
    });
  }
}

for (const [name, url] of [['publications', '/publications/'], ['researcher', '/researchers/bryce-hackel/'], ['lab', '/labs/ucsd-arc/']]) {
  test(name, async ({page, mock}, info) => {
    await page.goto(url);
    await expect(page.locator('#publication-list')).toContainText(paper.title);
    await expect(page.locator('#refresh')).toBeEnabled();
    await capture(page, info, name);
    await page.getByText('Abstract', {exact: true}).click();
    await expect(page.getByText(paper.abstract, {exact: true})).toBeVisible();
    await capture(page, info, `${name}-abstract`);
  });
}

for (const state of ['empty', 'error']) {
  test(`publications ${state}`, async ({page, mock}, info) => {
    mock.empty = state === 'empty'; mock.error = state === 'error';
    await page.goto('/publications/');
    await expect(page.locator(state === 'empty' ? '#publication-list' : '#sync-status'))
      .toContainText(state === 'empty' ? 'No publications' : 'Publications unavailable');
    await capture(page, info, `publications-${state}`);
  });
}

for (const state of ['signed-out', 'new-user', 'member', 'owner', 'admin']) {
  test(`account ${state}`, async ({page, mock}, info) => {
    mock.signedIn = state !== 'signed-out';
    mock.profile = !['signed-out', 'new-user'].includes(state);
    mock.role = mock.profile ? state : null;
    await page.goto('/account/');
    await accountReady(page);
    await expect(page.locator(mock.signedIn ? '#account' : '#login')).toBeVisible();
    await capture(page, info, `account-${state}`);
    if (mock.signedIn) {
      await page.getByText('Add a lab', {exact: true}).click();
      await expect(page.locator('#lab-name')).toBeVisible();
      await capture(page, info, `account-${state}-add-lab`);
    }
    if (['owner', 'admin'].includes(state)) {
      await page.getByRole('button', {name: 'Create invitation link'}).click();
      await expect(page.getByRole('textbox', {name: 'Lab invitation link'})).toHaveValue(/preview-token/);
      await capture(page, info, `account-${state}-invite-link`);
    }
  });
}

test('search choose author and select papers', async ({page, mock}, info) => {
  mock.signedIn = true;
  await page.goto('/account/');
  await accountReady(page);
  await page.getByLabel('Author or paper').fill('Bryce');
  await page.getByRole('button', {name: 'Find papers'}).click();
  await expect(page.locator('#author-choices')).toContainText('Choose your name');
  await capture(page, info, 'author-search');
  await page.getByRole('button', {name: 'Bryce Hackel', exact: true}).click();
  await expect(page.locator('#add')).toBeEnabled();
  await expect(page.locator('input[name="paper"]')).toBeChecked();
  await capture(page, info, 'papers-selected');
});

for (const state of ['signed-out', 'choose-author', 'join', 'expired']) {
  test(`invitation ${state}`, async ({page, mock}, info) => {
    mock.signedIn = ['choose-author', 'join'].includes(state);
    mock.profile = state === 'join'; mock.invalidInvite = state === 'expired';
    await page.goto('/account/?invite=preview-token');
    await accountReady(page);
    await expect(page.locator('#lab-invitation')).toBeVisible();
    // The base branch has no popup; PR #16 introduces it. Capture both versions.
    await capture(page, info, `invitation-${state}-initial`);
    const dialog = page.locator('#invitation-dialog[open]');
    if (await dialog.count()) {
      await dialog.getByRole('button', {name: 'Continue'}).click();
      await expect(dialog).toHaveCount(0);
    }
    const expected = {'signed-out': 'Sign in to join', 'choose-author': /choose your (author )?name/, join: 'Join lab', expired: 'Invitation not found'};
    await expect(page.locator('#lab-invitation')).toContainText(expected[state]);
    await capture(page, info, `invitation-${state}-card`);
    if (state === 'join') {
      await page.getByRole('button', {name: 'Join lab', exact: true}).click();
      await expect(page.locator('#lab-invitation')).toBeHidden();
      await expect(page.locator('#status')).toContainText('Joined');
      await capture(page, info, 'invitation-joined');
    }
  });
}


test('create a profile without publications', async ({page, mock}, info) => {
  mock.signedIn = true;
  await page.goto('/account/');
  await accountReady(page);
  test.skip(await page.locator('#profile-edit').count() === 0, 'Profile form was added in this revision.');
  await page.locator('#account summary').filter({hasText: 'Create or edit your researcher profile'}).click();
  await page.getByLabel('Your name', {exact: true}).fill('New Researcher');
  await page.getByLabel('Institution', {exact: true}).first().fill('Example University');
  await capture(page, info, 'profile-create');
  await page.getByRole('button', {name: 'Save profile', exact: true}).click();
  await expect(page.locator('#status')).toHaveText('Profile saved.');
  await expect(page.locator('#profile')).toHaveText('New Researcher');
});
