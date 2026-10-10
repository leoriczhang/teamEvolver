// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import {cleanup, fireEvent, render, screen, waitFor} from '@testing-library/react';
import {afterEach, describe, expect, it, vi} from 'vitest';
import {api} from '@/api/client';
import KeeMethodsView from './KeeMethodsView';
vi.mock('@/api/client', () => ({api: vi.fn()}));
const mocked = vi.mocked(api);
afterEach(() => {cleanup(); mocked.mockReset();});
describe('KEE methods governance', () => {
  it('sends structured JSON and suppresses double-click source submission', async () => {
    let finish!: (value: unknown) => void;
    mocked.mockImplementation((path) => path === '/kee/v1/sources'
      ? new Promise(resolve => {finish = resolve;}) : Promise.resolve(path.endsWith('releases') ? {items: []} : []));
    render(<KeeMethodsView active userId="reviewer" admin />);
    fireEvent.change(screen.getByLabelText('Viking 来源'), {target: {value: 'viking://resources/input'}});
    const button = screen.getByRole('button', {name: '冻结来源'});
    fireEvent.click(button); fireEvent.click(button);
    const submissions = mocked.mock.calls.filter(([path]) => path.endsWith('/sources'));
    expect(submissions).toHaveLength(1);
    expect(submissions[0][1]?.headers).toEqual({'Content-Type': 'application/json'});
    expect(JSON.parse(String(submissions[0][1]?.body))).toEqual({roots: ['viking://resources/input'], uploads: [], readers: ['reviewer']});
    finish({collection_id: 'frozen'});
    await waitFor(() => expect(screen.getByText(/来源集合：frozen/)).toBeVisible());
  });
  it('reader sees Wiki and feedback without governance controls', async () => {
    mocked.mockResolvedValue({items: []});
    render(<KeeMethodsView active userId="reader" admin={false} />);
    expect(screen.getByText('已发布 Wiki（只读）')).toBeVisible();
    expect(screen.queryByRole('button', {name: '冻结来源'})).not.toBeInTheDocument();
    expect(screen.queryByRole('button', {name: '发布 Wiki'})).not.toBeInTheDocument();
    await waitFor(() => expect(mocked).toHaveBeenCalled());
    expect(mocked.mock.calls.map(([path]) => path)).toEqual(['/kee/v1/wiki/releases']);
  });
});
