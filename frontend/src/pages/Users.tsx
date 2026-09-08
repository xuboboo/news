/**
 * 用户与租户管理：super_admin 全局管理；tenant_admin 仅本租户 member。
 * 数据源：/api/v2/admin/users*、/api/v2/admin/tenants*（见 backend/app/api/v2/users.py）。
 */
import { FormEvent, useCallback, useEffect, useState } from 'react';
import {
  Ban,
  Building2,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Copy,
  KeyRound,
  Loader2,
  Plus,
  RefreshCw,
  Search,
  ShieldCheck,
  ShieldX,
  UserCog,
  Users as UsersIcon,
  X,
} from 'lucide-react';
import { PageHeader } from '../components/common';
import { useModalA11y, usePageTitle } from '../utils/hooks';
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
const PAGE_SIZE = 50;

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

/** 轻量模态：Escape 关闭 + 遮罩点击关闭 + 滚动锁 + 焦点圈（useModalA11y） */
function Modal({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: React.ReactNode;
}) {
  const ref = useModalA11y(onClose);
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4" role="dialog" aria-modal="true" aria-label={title}>
      <div
        className="absolute inset-0 bg-[rgb(0_0_0/var(--overlay-alpha))] backdrop-blur-sm"
        onClick={onClose}
      />
      <div
        ref={ref}
        tabIndex={-1}
        className="card relative w-full max-w-md p-5 max-h-[85vh] overflow-y-auto outline-none"
      >
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
  // 创建成功后的「初始密码一次性展示」：模态里已可见，但关闭即不可再取，这里给一次明确留存机会
  const [createdPwd, setCreatedPwd] = useState<{ email: string; password: string } | null>(null);

  const [page, setPage] = useState(1);

  /** 拉取用户列表：可显式指定页码（翻页/重置查询时避免读到过期的 page 闭包值） */
  const loadUsers = useCallback(
    async (targetPage?: number, overrides?: { status?: string; tenant?: string }) => {
      if (!canManage) return;
      setListBusy(true);
      try {
        const status = overrides?.status ?? statusFilter;
        const tenant = overrides?.tenant ?? tenantFilter;
        const r = await getUsers({
          q: q || undefined,
          status: status || undefined,
          tenant_id: tenant ? Number(tenant) : undefined,
          page: targetPage ?? page,
          page_size: PAGE_SIZE,
        });
        setUsers(r.items ?? []);
        setTotal(r.total);
      } catch (e: any) {
        flash(false, `加载用户失败：${e?.message ?? e}`);
        setUsers([]);
      } finally {
        setListBusy(false);
      }
    },
    [canManage, q, statusFilter, tenantFilter, page, flash]
  );

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
    // eslint-disable-next-line react-hooks/set-state-in-effect -- 切 tab 拉取列表（异步数据加载）
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
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    setPage(1);
                    void loadUsers(1);
                  }
                }}
                placeholder="搜索邮箱 / 姓名，回车确认"
                className="w-full pl-8 pr-3 py-2 rounded-lg border border-radar-border bg-radar-panel text-xs text-slate-100 outline-none focus:border-accent/60"
              />
            </div>
            <select
              value={statusFilter}
              onChange={(e) => {
                setStatusFilter(e.target.value);
                setPage(1);
                void loadUsers(1, { status: e.target.value });
              }}
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
                onChange={(e) => {
                  setTenantFilter(e.target.value);
                  setPage(1);
                  void loadUsers(1, { tenant: e.target.value });
                }}
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
              onClick={() => {
                setPage(1);
                void loadUsers(1);
              }}
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

          {/* 分页 */}
          {total > PAGE_SIZE && (
            <div className="flex items-center justify-end gap-3 text-xs text-slate-400">
              <span>第 {page} / {Math.max(1, Math.ceil(total / PAGE_SIZE))} 页 · 共 {total} 个账号</span>
              <button
                onClick={() => {
                  const prev = Math.max(1, page - 1);
                  setPage(prev);
                  void loadUsers(prev);
                }}
                disabled={page <= 1}
                className="chip border border-radar-border bg-radar-card disabled:opacity-30 cursor-pointer"
                aria-label="上一页"
              >
                <ChevronLeft className="h-3.5 w-3.5" />
              </button>
              <button
                onClick={() => {
                  const next = Math.min(Math.ceil(total / PAGE_SIZE), page + 1);
                  setPage(next);
                  void loadUsers(next);
                }}
                disabled={page >= Math.ceil(total / PAGE_SIZE)}
                className="chip border border-radar-border bg-radar-card disabled:opacity-30 cursor-pointer"
                aria-label="下一页"
              >
                <ChevronRight className="h-3.5 w-3.5" />
              </button>
            </div>
          )}
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
          onCreated={(u, password) => {
            setCreateOpen(false);
            void loadUsers();
            setCreatedPwd({ email: u.email, password });
          }}
        />
      )}

      {/* ———— 初始密码一次性展示（关闭后只能重置，务必先复制留存） ———— */}
      {createdPwd && (
        <CreatedPasswordModal
          email={createdPwd.email}
          password={createdPwd.password}
          onClose={() => setCreatedPwd(null)}
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
}: {
  superAdmin: boolean;
  tenants: TenantItem[];
  myTenantId?: number;
  onClose: () => void;
  onCreated: (u: ManagedUser, password: string) => void;
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
      onCreated(r.item, password);
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

// ———————————————————— 初始密码一次性展示 ————————————————————

function CreatedPasswordModal({
  email,
  password,
  onClose,
}: {
  email: string;
  password: string;
  onClose: () => void;
}) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    const text = `账号：${email}\n初始密码：${password}`;
    try {
      // 非安全上下文（如 http://局域网IP）没有 clipboard API，降级为临时选区复制
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
      }
      setCopied(true);
      setTimeout(() => setCopied(false), 2500);
    } catch {
      /* 复制失败（权限拒绝等），用户可手动选择文本复制 */
    }
  };
  return (
    <Modal title="账号已创建" onClose={onClose}>
      <div className="space-y-4">
        <div className="rounded-lg border border-amber-400/30 bg-amber-500/5 px-3 py-2.5 text-xs text-amber-300 flex items-start gap-2">
          <ShieldCheck className="h-4 w-4 shrink-0 mt-px" />
          初始密码仅此一次展示，关闭后只能「重置密码」重新生成。请先复制留存并线下告知用户。
        </div>
        <div>
          <div className="text-xs text-slate-400 mb-1">登录账号</div>
          <div className="font-mono text-sm text-slate-100">{email}</div>
        </div>
        <div>
          <div className="text-xs text-slate-400 mb-1">初始密码（首登强制改密）</div>
          <div className="font-mono text-base text-slate-100 select-all tracking-wide">{password}</div>
        </div>
        <div className="flex justify-end gap-2 pt-1">
          <button
            onClick={() => void copy()}
            className="px-3 py-2 rounded-lg border border-accent/40 text-xs text-accent hover:bg-accent/10 flex items-center gap-1.5 cursor-pointer"
          >
            <Copy className="h-3.5 w-3.5" />
            {copied ? '已复制' : '复制账号和密码'}
          </button>
          <button
            onClick={onClose}
            className="px-4 py-2 rounded-lg bg-accent/90 hover:bg-accent text-white text-xs font-medium cursor-pointer"
          >
            我已留存，关闭
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
        />
      )}
    </section>
  );
}

function CreateTenantModal({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: () => void;
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
