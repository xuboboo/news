// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import Login from './Login';

const auth = vi.hoisted(() => ({
  login: vi.fn(),
  setAuthed: vi.fn(),
  useAuth: vi.fn(),
}));

vi.mock('../api/auth', () => ({
  login: auth.login,
  ApiError: class ApiError extends Error { status = 0; },
}));
vi.mock('../auth', () => ({ setAuthed: auth.setAuthed, useAuth: auth.useAuth }));
vi.mock('../components/Layout', () => ({ ThemeToggle: () => null }));

function mount(from: string) {
  const router = createMemoryRouter([
    { path: '/login', element: <Login /> },
    { path: '/hot', element: <div>热榜首页</div> },
    { path: '/events/:id', element: <div>事件详情</div> },
    { path: '*', element: <div>未找到</div> },
  ], { initialEntries: [{ pathname: '/login', state: { from } }] });
  return { router, ...render(<RouterProvider router={router} />) };
}

describe('Login return navigation', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    auth.useAuth.mockReturnValue({ status: 'anon', user: null });
    auth.login.mockResolvedValue({
      id: 1, email: 'member@example.test', display_name: '测试账号', role: 'member',
      tenant_id: 1, must_change_password: false, last_login_at: null,
    });
  });
  afterEach(() => cleanup());

  it.each(['/\\\\attacker.example', '//attacker.example', 'https://attacker.example'])('uses the safe home route for an untrusted return path: %s', async (from) => {
      mount(from);
      fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'member@example.test' } });
      fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'safe-test-password' } });
      fireEvent.click(screen.getByRole('button', { name: '进入雷达' }));
      expect(await screen.findByText('热榜首页')).toBeTruthy();
      await waitFor(() => expect(auth.setAuthed).toHaveBeenCalledOnce());
    });

  it('preserves a valid internal destination after login', async () => {
    mount('/events/42');
    fireEvent.change(screen.getByLabelText('邮箱'), { target: { value: 'member@example.test' } });
    fireEvent.change(screen.getByLabelText('密码'), { target: { value: 'safe-test-password' } });
    fireEvent.click(screen.getByRole('button', { name: '进入雷达' }));
    expect(await screen.findByText('事件详情')).toBeTruthy();
  });
});
