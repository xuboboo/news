/**
 * 用户与租户管理：super_admin 全局管理；tenant_admin 仅本租户 member。
 * 数据源：/api/v2/admin/users*、/api/v2/admin/tenants*（见 backend/app/api/v2/users.py）。
 */
import { FormEvent, useCallback, useEffect, useState } from 'react';
import {
  Ban,
  Building2,
  CheckCircle2,
  KeyRound,
  Loader2,
  Plus,
  RefreshCw,
  Search,
  ShieldX,
  UserCog,
  Users as UsersIcon,
  X,
} from 'lucide-react';
import { PageHeader } from '../components/common';
import { usePageTitle } from '../utils/hooks';
import { ROLE_LABELS, isSuperAdmin, isTenantAdmin, useAuth, type Role } from '../auth';
import {
  ManagedUser,
  TenantItem,
  createTenant,
  createUser,
  getTenants,
  getUsers,
  resetUserPassword,
  updateTenant,
  updateUser,
} from '../api/admin';

const ROLE_OPTIONS: Role[] = ['member', 'tenant_admin', 'super_admin'];

function genPassword(len = 12): string {
  const chars = 'abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789';
  const arr = new Uint32Array(len);
  crypto.getRandomValues(arr);
  return Array.from(arr, (n) => chars[n % chars.length]).join('');
}

function fmtTime(iso: string | null): string {
  if (!iso) return '—';
  return new Date(iso).toLocaleString('zh-CN', { hour12: false });
}

/** 轻量模态：Escape 关闭 + 遮罩点击关闭 + 滚动锁 */
function Modal({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: React.ReactNode;
}) {
  useEffect(() => {
    const prev = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', onKey);
    return () => {
      document.body.style.overflow = prev;
      document.removeEventListener('keydown', onKey);
    };
  }, [onClose]);
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4" role="dialog" aria-modal="true" aria-label={title}>
      <div
        className="absolute inset-0 bg-[rgb(0_0_0/var(--overlay-alpha))] backdrop-blur-sm"
        onClick={onClose}
      />
      <div className="card relative w-full max-w-md p-5 max-h-[85vh] overflow-y-auto">
        <div className="flex items-center justify-between mb-4">
          <div className="text-sm font-semibold text-slate-100">{title}</div>
          <button
            onClick={onClose}
            aria-label="关闭"
            className="p-1 rounded-lg text-slate-500 hover:text-slate-200 hover:bg-radar-panel cursor-pointer"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}

const inputCls =
  'w-full rounded-lg border border-radar-border bg-radar-panel px-3 py-2 text-sm text-slate-100 outline-none focus:border-accent/60';

export default function Users() {
  usePageTitle('用户与租户');
  const { user: me } = useAuth();
  const superAdmin = isSuperAdmin(me);
  const canManage = isTenantAdmin(me);

  const [tab, setTab] = useState<'users' | 'tenants'>('users');
  const [toast, setToast] = useState<{ ok: boolean; text: string } | null>(null);
  const flash = useCallback((ok: boolean, text: string) => {
    setToast({ ok, text });
    setTimeout(() => setToast(null), 4000);
  }, []);

  // ———— 用户列表状态 ————
  const [users, setUsers] = useState<ManagedUser[] | null>(null);
  const [total, setTotal] = useState(0);
  const [tenants, setTenants] = useState<TenantItem[]>([]);
  const [q, setQ] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [tenantFilter, setTenantFilter] = useState('');
  const [listBusy, setListBusy] = useState(false);

  // ———— 弹层状态 ————
  const [createOpen, setCreateOpen] = useState(false);
  const [resetTarget, setResetTarget] = useState<ManagedUser | null>(null);

  const loadUsers = useCallback(async () => {
    if (!canManage) return;
    setListBusy(true);
    try {
      const r = await getUsers({
        q: q || undefined,
        status: statusFilter || undefined,
        tenant_id: tenantFilter ? Number(tenantFilter) : undefined,
      });
      setUsers(r.items ?? []);
      setTotal(r.total);
    } catch (e: any) {
      flash(false, `加载用户失败：${e?.message ?? e}`);
      setUsers([]);
    } finally {
      setListBusy(false);
    }
  }, [canManage, q, statusFilter, tenantFilter, flash]);

  const loadTenants = useCallback(async () => {
    if (!superAdmin) return;
    try {
      const r = await getTenants();
      setTenants(r.items ?? []);
    } catch {
      /* 租户列表失败不阻塞用户页 */
    }
  }, [superAdmin]);

  useEffect(() => {
    if (canManage) void loadUsers();
    if (superAdmin) void loadTenants();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab]);

  if (!canManage) {
    return (
      <div>
        <PageHeader title="用户与租户" description="账号 · 组织管理" />
        <div className="max-w-md card p-8 mx-auto mt-16 text-center">
          <div className="mx-auto mb-4 w-fit p-3 rounded-xl bg-red-500/10 text-red-400">
            <ShieldX className="h-7 w-7" />
          </div>
          <div className="text-sm font-semibold text-slate-100 mb-1.5">无访问权限</div>
          <p className="text-xs text-slate-500 leading-5">用户与租户管理仅对管理员开放。</p>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <PageHeader title="用户与租户" description={`账号与组织管理 · 共 ${total} 个账号`}>
        <button
          onClick={() => {
            void (tab === 'users' ? loadUsers() : loadTenants());
          }}
          className="chip border border-radar-border bg-radar-card text-slate-300 hover:text-accent hover:border-accent/40 cursor-pointer"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${listBusy ? 'animate-spin' : ''}`} /> 刷新
        </button>
        {tab === 'users' ? (
          <button
            onClick={() => setCreateOpen(true)}
            className="chip bg-accent/90 hover:bg-accent text-white border border-accent/50 cursor-pointer"
          >
            <Plus className="h-3.5 w-3.5" /> 新建用户
          </button>
        ) : null}
      </PageHeader>

      {toast && (
        <div
          className={`card px-4 py-2.5 text-sm flex items-center gap-2 ${
            toast.ok ? 'text-emerald-300' : 'text-red-300'
          }`}
        >
          {toast.ok ? <CheckCircle2 className="h-4 w-4" /> : <ShieldX className="h-4 w-4" />}
          {toast.text}
        </div>
      )}

      {/* ———— Tab 切换（macOS 分段控件） ———— */}
      <div className="seg w-fit" role="tablist">
        <button
          role="tab"
          aria-selected={tab === 'users'}
          onClick={() => setTab('users')}
          className={`seg-item ${tab === 'users' ? 'seg-item-active' : ''}`}
        >
          <UsersIcon className="h-3.5 w-3.5" /> 用户管理
        </button>
        {superAdmin && (
          <button
            role="tab"
            aria-selected={tab === 'tenants'}
            onClick={() => setTab('tenants')}
            className={`seg-item ${tab === 'tenants' ? 'seg-item-active' : ''}`}
          >
            <Building2 className="h-3.5 w-3.5" /> 租户管理
          </button>
        )}
      </div>

      {tab === 'users' && (
        <section className="space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <div className="relative flex-1 min-w-[12rem] max-w-xs">
              <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-slate-500" />
              <input
                value={q}
                onChange={(e) => setQ(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && void loadUsers()}
                placeholder="搜索邮箱 / 姓名，回车确认"
                className="w-full pl-8 pr-3 py-2 rounded-lg border border-radar-border bg-radar-panel text-xs text-slate-100 outline-none focus:border-accent/60"
              />
            </div>
            <select
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value)}
              className="rounded-lg border border-radar-border bg-radar-panel px-2.5 py-2 text-xs text-slate-200 outline-none focus:border-accent/60 cursor-pointer"
              aria-label="状态筛选"
            >
              <option value="">全部状态</option>
              <option value="active">启用</option>
              <option value="disabled">停用</option>
            </select>
            {superAdmin && (
              <select
                value={tenantFilter}
                onChange={(e) => setTenantFilter(e.target.value)}
                className="rounded-lg border border-radar-border bg-radar-panel px-2.5 py-2 text-xs text-slate-200 outline-none focus:border-accent/60 cursor-pointer"
                aria-label="租户筛选"
              >
                <option value="">全部租户</option>
                {tenants.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.name}
                  </option>
                ))}
              </select>
            )}
            <button
              onClick={() => void loadUsers()}
              className="chip border border-radar-border bg-radar-card text-slate-300 hover:text-accent cursor-pointer"
            >
              查询
            </button>
          </div>

          <div className="card overflow-hidden">
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-slate-500 border-b border-radar-border">
                    <th className="text-left font-medium px-4 py-2.5">邮箱</th>
                    <th className="text-left font-medium px-2 py-2.5">姓名</th>
                    <th className="text-left font-medium px-2 py-2.5">角色</th>
                    <th className="text-left font-medium px-2 py-2.5">租户</th>
                    <th className="text-left font-medium px-2 py-2.5">状态</th>
                    <th className="text-left font-medium px-2 py-2.5">最近登录</th>
                    <th className="text-left font-medium px-4 py-2.5">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {users === null || listBusy ? (
                    <tr>
                      <td colSpan={7} className="px-4 py-10 text-center">
                        <Loader2 className="h-5 w-5 animate-spin text-slate-500 mx-auto" />
                      </td>
                    </tr>
                  ) : users.length === 0 ? (
                    <tr>
                      <td colSpan={7} className="px-4 py-8 text-center text-slate-500">
                        暂无账号，点右上角「新建用户」创建第一个账号
                      </td>
                    </tr>
                  ) : (
                    users.map((u) => (
                      <UserRow
                        key={u.id}
                        u={u}
                        superAdmin={superAdmin}
                        isMe={u.id === me?.id}
                        onFlash={flash}
                        onReset={() => setResetTarget(u)}
                        onChanged={() => void loadUsers()}
                      />
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </section>
      )}

      {tab === 'tenants' && superAdmin && (
        <TenantsPanel
          tenants={tenants}
          onReload={() => void loadTenants()}
          onFlash={flash}
        />
      )}

      {/* ———— 新建用户弹层 ———— */}
      {createOpen && (
        <CreateUserModal
          superAdmin={superAdmin}
          tenants={tenants}
          myTenantId={me?.tenant_id}
          onClose={() => setCreateOpen(false)}
          onCreated={(u) => {
            setCreateOpen(false);
            void loadUsers();
            flash(true, `已创建 ${u.email}，初始密码仅此一次展示，请立即线下告知用户`);
          }}
          onFlash={flash}
        />
      )}

      {/* ———— 重置密码弹层 ———— */}
      {resetTarget && (
        <ResetPasswordModal
          target={resetTarget}
          onClose={() => setResetTarget(null)}
          onFlash={flash}
        />
      )}
    </div>
  );
}

// ———————————————————— 用户行 ————————————————————

function UserRow({
  u,
  superAdmin,
  isMe,
  onFlash,
  onReset,
  onChanged,
}: {
  u: ManagedUser;
  superAdmin: boolean;
  isMe: boolean;
  onFlash: (ok: boolean, text: string) => void;
  onReset: () => void;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);

  const toggleStatus = async () => {
    if (busy) return;
    const next = u.status === 'active' ? 'disabled' : 'active';
    if (next === 'disabled' && !window.confirm(`确定停用 ${u.email}？停用后其所有会话立即失效`)) return;
    setBusy(true);
    try {
      await updateUser(u.id, { status: next });
      onFlash(true, next === 'disabled' ? `已停用 ${u.email}` : `已启用 ${u.email}`);
      onChanged();
    } catch (e: any) {
      onFlash(false, e?.message ?? '操作失败');
    } finally {
      setBusy(false);
    }
  };

  const changeRole = async (role: string) => {
    if (busy || role === u.role) return;
    setBusy(true);
    try {
      await updateUser(u.id, { role });
      onFlash(true, `已将 ${u.email} 角色调整为 ${ROLE_LABELS[role as Role]}`);
      onChanged();
    } catch (e: any) {
      onFlash(false, e?.message ?? '操作失败');
      onChanged();
    } finally {
      setBusy(false);
    }
  };

  return (
    <tr className="border-b border-radar-border/50 hover:bg-radar-panel/50">
      <td className="px-4 py-2.5 text-slate-200">
        {u.email}
        {u.must_change_password && (
          <span className="ml-1.5 text-[10px] text-amber-400" title="初始密码未更换">
            未改密
          </span>
        )}
      </td>
      <td className="px-2 py-2.5 text-slate-300">{u.display_name || '—'}</td>
      <td className="px-2 py-2.5">
        {superAdmin ? (
          <select
            value={u.role}
            onChange={(e) => void changeRole(e.target.value)}
            disabled={busy}
            aria-label={`调整 ${u.email} 的角色`}
            className="rounded-md border border-radar-border bg-radar-panel px-1.5 py-1 text-[11px] text-slate-200 outline-none focus:border-accent disabled:opacity-50 cursor-pointer"
          >
            {ROLE_OPTIONS.map((r) => (
              <option key={r} value={r}>
                {ROLE_LABELS[r]}
              </option>
            ))}
          </select>
        ) : (
          <span className="chip text-[10px] bg-slate-500/10 text-slate-300">{ROLE_LABELS[u.role]}</span>
        )}
      </td>
      <td className="px-2 py-2.5 text-slate-400">{u.tenant_name ?? u.tenant_id}</td>
      <td className="px-2 py-2.5">
        {u.status === 'active' ? (
          <span className="chip text-[10px] bg-emerald-500/10 text-emerald-300 border border-emerald-400/20">启用</span>
        ) : (
          <span className="chip text-[10px] bg-red-500/10 text-red-300 border border-red-400/20">停用</span>
        )}
      </td>
      <td className="px-2 py-2.5 text-slate-400 whitespace-nowrap">{fmtTime(u.last_login_at)}</td>
      <td className="px-4 py-2.5">
        <div className="flex items-center gap-1">
          <button
            onClick={onReset}
            title="重置密码（生成新的首登临时密码）"
            className="p-1.5 rounded-lg text-slate-500 hover:text-accent hover:bg-radar-panel cursor-pointer"
          >
            <KeyRound className="h-3.5 w-3.5" />
          </button>
          {u.status === 'active' ? (
            <button
              onClick={() => void toggleStatus()}
              disabled={busy || isMe}
              title={isMe ? '不能停用自己' : '停用账号'}
              className="p-1.5 rounded-lg text-slate-500 hover:text-red-300 hover:bg-radar-panel disabled:opacity-30 disabled:cursor-not-allowed cursor-pointer"
            >
              {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Ban className="h-3.5 w-3.5" />}
            </button>
          ) : (
            <button
              onClick={() => void toggleStatus()}
              disabled={busy}
              title="启用账号"
              className="p-1.5 rounded-lg text-slate-500 hover:text-emerald-300 hover:bg-radar-panel disabled:opacity-30 cursor-pointer"
            >
              {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <CheckCircle2 className="h-3.5 w-3.5" />}
            </button>
          )}
        </div>
      </td>
    </tr>
  );
}

// ———————————————————— 新建用户弹层 ————————————————————

function CreateUserModal({
  superAdmin,
  tenants,
  myTenantId,
  onClose,
  onCreated,
  onFlash,
}: {
  superAdmin: boolean;
  tenants: TenantItem[];
  myTenantId?: number;
  onClose: () => void;
  onCreated: (u: ManagedUser) => void;
  onFlash: (ok: boolean, text: string) => void;
}) {
  const [email, setEmail] = useState('');
  const [name, setName] = useState('');
  const [role, setRole] = useState<Role>('member');
  const [tenantId, setTenantId] = useState<string>(superAdmin ? String(tenants[0]?.id ?? '') : String(myTenantId ?? ''));
  const [password, setPassword] = useState(() => genPassword());
  const [showPwd, setShowPwd] = useState(true);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (busy) return;
    setErr('');
    setBusy(true);
    try {
      const r = await createUser({
        email: email.trim(),
        password,
        display_name: name.trim(),
        role,
        tenant_id: Number(tenantId),
      });
      onCreated(r.item);
    } catch (e: any) {
      setErr(e?.message ?? '创建失败');
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal title="新建用户" onClose={onClose}>
      <form onSubmit={submit} className="space-y-3.5">
        <label className="block">
          <span className="block text-xs text-slate-400 mb-1.5">邮箱（登录账号）</span>
          <input
            type="email"
            autoFocus
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="name@company.com"
            className={inputCls}
          />
        </label>
        <label className="block">
          <span className="block text-xs text-slate-400 mb-1.5">姓名（可选）</span>
          <input value={name} onChange={(e) => setName(e.target.value)} className={inputCls} />
        </label>
        <div className="grid grid-cols-2 gap-3">
          <label className="block">
            <span className="block text-xs text-slate-400 mb-1.5">角色</span>
            {superAdmin ? (
              <select
                value={role}
                onChange={(e) => setRole(e.target.value as Role)}
                className={inputCls + ' cursor-pointer'}
              >
                {ROLE_OPTIONS.map((r) => (
                  <option key={r} value={r}>
                    {ROLE_LABELS[r]}
                  </option>
                ))}
              </select>
            ) : (
              <input value="成员" disabled className={inputCls + ' opacity-60'} />
            )}
          </label>
          <label className="block">
            <span className="block text-xs text-slate-400 mb-1.5">所属租户</span>
            {superAdmin ? (
              <select
                value={tenantId}
                onChange={(e) => setTenantId(e.target.value)}
                className={inputCls + ' cursor-pointer'}
              >
                {tenants.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.name}
                  </option>
                ))}
              </select>
            ) : (
              <input value="本租户" disabled className={inputCls + ' opacity-60'} />
            )}
          </label>
        </div>
        <label className="block">
          <span className="block text-xs text-slate-400 mb-1.5">初始密码（用户首登强制改密）</span>
          <div className="flex gap-2">
            <input
              type={showPwd ? 'text' : 'password'}
              required
              minLength={8}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className={inputCls + ' font-mono'}
            />
            <button
              type="button"
              onClick={() => setPassword(genPassword())}
              title="随机生成"
              className="px-2.5 rounded-lg border border-radar-border bg-radar-card text-slate-300 hover:text-accent text-xs cursor-pointer whitespace-nowrap"
            >
              随机
            </button>
            <button
              type="button"
              onClick={() => setShowPwd((v) => !v)}
              className="px-2.5 rounded-lg border border-radar-border bg-radar-card text-slate-300 hover:text-accent text-xs cursor-pointer"
            >
              {showPwd ? '隐藏' : '显示'}
            </button>
          </div>
        </label>
        {err && <div className="text-xs text-red-400">{err}</div>}
        <div className="flex justify-end gap-2 pt-1">
          <button type="button" onClick={onClose} className="px-3 py-2 rounded-lg border border-radar-border text-xs text-slate-300 hover:bg-radar-panel cursor-pointer">
            取消
          </button>
          <button
            type="submit"
            disabled={busy || !email}
            className="px-4 py-2 rounded-lg bg-accent/90 hover:bg-accent text-white text-xs font-medium disabled:opacity-40 disabled:cursor-not-allowed flex items-center gap-1.5 cursor-pointer"
          >
            {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
            创建用户
          </button>
        </div>
      </form>
    </Modal>
  );
}

// ———————————————————— 重置密码弹层 ————————————————————

function ResetPasswordModal({
  target,
  onClose,
  onFlash,
}: {
  target: ManagedUser;
  onClose: () => void;
  onFlash: (ok: boolean, text: string) => void;
}) {
  const [password, setPassword] = useState(() => genPassword());
  const [showPwd, setShowPwd] = useState(true);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');

  const submit = async () => {
    if (busy) return;
    setErr('');
    setBusy(true);
    try {
      await resetUserPassword(target.id, password);
      onFlash(true, `已重置 ${target.email} 的密码：${password}（请线下告知，首登强制改密）`);
      onClose();
    } catch (e: any) {
      setErr(e?.message ?? '重置失败');
      setBusy(false);
    }
  };

  return (
    <Modal title={`重置密码 · ${target.email}`} onClose={onClose}>
      <div className="space-y-3.5">
        <label className="block">
          <span className="block text-xs text-slate-400 mb-1.5">新临时密码（首登强制改密）</span>
          <div className="flex gap-2">
            <input
              type={showPwd ? 'text' : 'password'}
              autoFocus
              minLength={8}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className={inputCls + ' font-mono'}
            />
            <button
              type="button"
              onClick={() => setPassword(genPassword())}
              className="px-2.5 rounded-lg border border-radar-border bg-radar-card text-slate-300 hover:text-accent text-xs cursor-pointer whitespace-nowrap"
            >
              随机
            </button>
            <button
              type="button"
              onClick={() => setShowPwd((v) => !v)}
              className="px-2.5 rounded-lg border border-radar-border bg-radar-card text-slate-300 hover:text-accent text-xs cursor-pointer"
            >
              {showPwd ? '隐藏' : '显示'}
            </button>
          </div>
        </label>
        <div className="text-[11px] text-slate-500 flex items-start gap-1.5">
          <UserCog className="h-3.5 w-3.5 shrink-0 mt-px" />
          重置后该用户的所有登录会话立即失效，需用新密码重新登录。
        </div>
        {err && <div className="text-xs text-red-400">{err}</div>}
        <div className="flex justify-end gap-2 pt-1">
          <button onClick={onClose} className="px-3 py-2 rounded-lg border border-radar-border text-xs text-slate-300 hover:bg-radar-panel cursor-pointer">
            取消
          </button>
          <button
            onClick={() => void submit()}
            disabled={busy || password.length < 8}
            className="px-4 py-2 rounded-lg bg-accent/90 hover:bg-accent text-white text-xs font-medium disabled:opacity-40 disabled:cursor-not-allowed flex items-center gap-1.5 cursor-pointer"
          >
            {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
            确认重置
          </button>
        </div>
      </div>
    </Modal>
  );
}

// ———————————————————— 租户管理面板（超管） ————————————————————

function TenantsPanel({
  tenants,
  onReload,
  onFlash,
}: {
  tenants: TenantItem[];
  onReload: () => void;
  onFlash: (ok: boolean, text: string) => void;
}) {
  const [createOpen, setCreateOpen] = useState(false);
  const [busyId, setBusyId] = useState<number | null>(null);

  const toggle = async (t: TenantItem) => {
    const next = t.status === 'active' ? 'disabled' : 'active';
    if (next === 'disabled' && !window.confirm(`确定停用租户「${t.name}」？该租户下全部账号将无法登录`)) return;
    setBusyId(t.id);
    try {
      await updateTenant(t.id, { status: next });
      onFlash(true, next === 'disabled' ? `已停用租户「${t.name}」` : `已启用租户「${t.name}」`);
      onReload();
    } catch (e: any) {
      onFlash(false, e?.message ?? '操作失败');
    } finally {
      setBusyId(null);
    }
  };

  return (
    <section className="space-y-3">
      <div className="flex justify-end">
        <button
          onClick={() => setCreateOpen(true)}
          className="chip bg-accent/90 hover:bg-accent text-white border border-accent/50 cursor-pointer"
        >
          <Plus className="h-3.5 w-3.5" /> 新建租户
        </button>
      </div>
      <div className="card overflow-hidden">
        <table className="w-full text-xs">
          <thead>
            <tr className="text-slate-500 border-b border-radar-border">
              <th className="text-left font-medium px-4 py-2.5">租户名称</th>
              <th className="text-left font-medium px-2 py-2.5">成员数</th>
              <th className="text-left font-medium px-2 py-2.5">状态</th>
              <th className="text-left font-medium px-2 py-2.5">备注</th>
              <th className="text-left font-medium px-2 py-2.5">创建时间</th>
              <th className="text-left font-medium px-4 py-2.5">操作</th>
            </tr>
          </thead>
          <tbody>
            {tenants.map((t) => (
              <tr key={t.id} className="border-b border-radar-border/50 hover:bg-radar-panel/50">
                <td className="px-4 py-2.5 text-slate-200">{t.name}</td>
                <td className="px-2 py-2.5 text-slate-400">{t.user_count}</td>
                <td className="px-2 py-2.5">
                  {t.status === 'active' ? (
                    <span className="chip text-[10px] bg-emerald-500/10 text-emerald-300 border border-emerald-400/20">启用</span>
                  ) : (
                    <span className="chip text-[10px] bg-red-500/10 text-red-300 border border-red-400/20">停用</span>
                  )}
                </td>
                <td className="px-2 py-2.5 text-slate-400 max-w-xs truncate">{t.remark || '—'}</td>
                <td className="px-2 py-2.5 text-slate-400 whitespace-nowrap">{fmtTime(t.created_at)}</td>
                <td className="px-4 py-2.5">
                  <button
                    onClick={() => void toggle(t)}
                    disabled={busyId === t.id}
                    className="text-[11px] text-slate-400 hover:text-red-300 disabled:opacity-40 cursor-pointer"
                  >
                    {busyId === t.id ? '处理中…' : t.status === 'active' ? '停用' : '启用'}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {createOpen && (
        <CreateTenantModal
          onClose={() => setCreateOpen(false)}
          onCreated={() => {
            setCreateOpen(false);
            onReload();
            onFlash(true, '租户已创建');
          }}
          onFlash={onFlash}
        />
      )}
    </section>
  );
}

function CreateTenantModal({
  onClose,
  onCreated,
  onFlash,
}: {
  onClose: () => void;
  onCreated: () => void;
  onFlash: (ok: boolean, text: string) => void;
}) {
  const [name, setName] = useState('');
  const [remark, setRemark] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (busy) return;
    setErr('');
    setBusy(true);
    try {
      await createTenant({ name: name.trim(), remark: remark.trim() });
      onCreated();
    } catch (e: any) {
      setErr(e?.message ?? '创建失败');
      setBusy(false);
    }
  };

  return (
    <Modal title="新建租户" onClose={onClose}>
      <form onSubmit={submit} className="space-y-3.5">
        <label className="block">
          <span className="block text-xs text-slate-400 mb-1.5">租户名称</span>
          <input autoFocus required maxLength={64} value={name} onChange={(e) => setName(e.target.value)} className={inputCls} placeholder="如：编辑部" />
        </label>
        <label className="block">
          <span className="block text-xs text-slate-400 mb-1.5">备注（可选）</span>
          <input maxLength={255} value={remark} onChange={(e) => setRemark(e.target.value)} className={inputCls} />
        </label>
        {err && <div className="text-xs text-red-400">{err}</div>}
        <div className="flex justify-end gap-2 pt-1">
          <button type="button" onClick={onClose} className="px-3 py-2 rounded-lg border border-radar-border text-xs text-slate-300 hover:bg-radar-panel cursor-pointer">
            取消
          </button>
          <button
            type="submit"
            disabled={busy || !name.trim()}
            className="px-4 py-2 rounded-lg bg-accent/90 hover:bg-accent text-white text-xs font-medium disabled:opacity-40 disabled:cursor-not-allowed flex items-center gap-1.5 cursor-pointer"
          >
            {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
            创建
          </button>
        </div>
      </form>
    </Modal>
  );
}
