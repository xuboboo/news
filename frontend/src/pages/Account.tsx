/**
 * 账号设置页：修改密码 + 我的活跃会话管理。
 * 首登强制改密（must_change_password）时 RequireAuth 会把用户锁在本页，直到改密完成。
 */
import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  CheckCircle2,
  KeyRound,
  Loader2,
  LogOut,
  MonitorSmartphone,
  ShieldAlert,
  Trash2,
} from 'lucide-react';
import { PageHeader } from '../components/common';
import { usePageTitle } from '../utils/hooks';
import { markPasswordChanged, useAuth } from '../auth';
import {
  SessionItem,
  changePassword,
  getMySessions,
  logout as authLogout,
  revokeSession,
} from '../api/auth';

export default function Account() {
  usePageTitle('账号设置');
  const { user } = useAuth();
  const navigate = useNavigate();
  const mustChange = !!user?.must_change_password;

  // ———— 改密表单 ————
  const [oldPwd, setOldPwd] = useState('');
  const [newPwd, setNewPwd] = useState('');
  const [newPwd2, setNewPwd2] = useState('');
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  // ———— 会话列表 ————
  const [sessions, setSessions] = useState<SessionItem[] | null>(null);
  const [revoking, setRevoking] = useState<number | null>(null);

  const loadSessions = useCallback(async () => {
    try {
      const r = await getMySessions();
      setSessions(r.items ?? []);
    } catch {
      setSessions([]);
    }
  }, []);

  useEffect(() => {
    if (!mustChange) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- 挂载拉取会话列表（异步数据加载）
      void loadSessions();
    }
  }, [mustChange, loadSessions]);

  const clientOf = (ua: string): string => {
    if (ua.includes('Edg/')) return 'Edge';
    if (ua.includes('Chrome/')) return 'Chrome';
    if (ua.includes('Firefox/')) return 'Firefox';
    if (ua.includes('Safari/') && !ua.includes('Chrome')) return 'Safari';
    return ua ? '其他浏览器' : '未知客户端';
  };

  const submitChange = async () => {
    if (saving) return;
    setMsg(null);
    if (!oldPwd || !newPwd) {
      setMsg({ ok: false, text: '请填写当前密码与新密码' });
      return;
    }
    if (newPwd.length < 8) {
      setMsg({ ok: false, text: '新密码至少 8 位' });
      return;
    }
    if (newPwd !== newPwd2) {
      setMsg({ ok: false, text: '两次输入的新密码不一致' });
      return;
    }
    setSaving(true);
    try {
      await changePassword(oldPwd, newPwd);
      markPasswordChanged();
      setOldPwd('');
      setNewPwd('');
      setNewPwd2('');
      setMsg({ ok: true, text: '密码已修改；其他设备的会话已全部吊销' });
      void loadSessions();
    } catch (e: any) {
      setMsg({ ok: false, text: e?.message ?? '修改失败' });
    } finally {
      setSaving(false);
    }
  };

  const doRevoke = async (id: number) => {
    setRevoking(id);
    try {
      await revokeSession(id);
      await loadSessions();
    } catch {
      /* 失败静默，列表刷新可见 */
    } finally {
      setRevoking(null);
    }
  };

  const doLogoutAll = async () => {
    try {
      await authLogout();
    } finally {
      navigate('/login', { replace: true });
    }
  };

  return (
    <div className="space-y-6 max-w-2xl">
      <PageHeader title="账号设置" description="修改密码 · 管理我的登录会话" />

      {mustChange && (
        <div className="card px-4 py-3 border-amber-500/30 bg-amber-500/5 text-sm text-amber-200 flex items-start gap-2">
          <ShieldAlert className="h-4 w-4 mt-0.5 shrink-0" />
          <div>
            首次登录（或密码被重置）：请先设置新密码，设置完成后才能使用其他功能。
          </div>
        </div>
      )}

      {/* ———— 修改密码 ———— */}
      <section className="card p-5">
        <h2 className="text-sm font-semibold text-slate-200 flex items-center gap-2 mb-4">
          <KeyRound className="h-4 w-4 text-accent" /> 修改密码
        </h2>
        <div className="space-y-3 max-w-sm">
          <label className="block">
            <span className="block text-xs text-slate-400 mb-1.5">当前密码</span>
            <input
              type="password"
              autoComplete="current-password"
              value={oldPwd}
              onChange={(e) => setOldPwd(e.target.value)}
              className="w-full rounded-lg border border-radar-border bg-radar-panel px-3 py-2 text-sm text-slate-100 outline-none focus:border-accent/60"
            />
          </label>
          <label className="block">
            <span className="block text-xs text-slate-400 mb-1.5">新密码（至少 8 位）</span>
            <input
              type="password"
              autoComplete="new-password"
              value={newPwd}
              onChange={(e) => setNewPwd(e.target.value)}
              className="w-full rounded-lg border border-radar-border bg-radar-panel px-3 py-2 text-sm text-slate-100 outline-none focus:border-accent/60"
            />
          </label>
          <label className="block">
            <span className="block text-xs text-slate-400 mb-1.5">确认新密码</span>
            <input
              type="password"
              autoComplete="new-password"
              value={newPwd2}
              onChange={(e) => setNewPwd2(e.target.value)}
              className="w-full rounded-lg border border-radar-border bg-radar-panel px-3 py-2 text-sm text-slate-100 outline-none focus:border-accent/60"
            />
          </label>
          {msg && (
            <div
              className={`text-xs flex items-center gap-1.5 ${msg.ok ? 'text-emerald-300' : 'text-red-400'}`}
            >
              {msg.ok ? <CheckCircle2 className="h-3.5 w-3.5" /> : <ShieldAlert className="h-3.5 w-3.5" />}
              {msg.text}
            </div>
          )}
          <button
            onClick={() => void submitChange()}
            disabled={saving || !oldPwd || !newPwd || !newPwd2}
            className="w-full sm:w-auto px-4 py-2 rounded-lg bg-accent/90 hover:bg-accent text-white text-sm font-medium disabled:opacity-40 disabled:cursor-not-allowed flex items-center justify-center gap-2 cursor-pointer"
          >
            {saving && <Loader2 className="h-4 w-4 animate-spin" />}
            {saving ? '保存中…' : '保存新密码'}
          </button>
        </div>
      </section>

      {/* ———— 我的会话 ———— */}
      {!mustChange && (
        <section className="card p-5">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-sm font-semibold text-slate-200 flex items-center gap-2">
              <MonitorSmartphone className="h-4 w-4 text-accent" /> 我的登录会话
            </h2>
            <button
              onClick={() => void loadSessions()}
              className="text-xs text-slate-500 hover:text-accent cursor-pointer"
            >
              刷新
            </button>
          </div>
          {sessions === null ? (
            <div className="py-6 flex justify-center">
              <Loader2 className="h-5 w-5 animate-spin text-slate-500" />
            </div>
          ) : sessions.length === 0 ? (
            <div className="text-xs text-slate-500 py-4 text-center">暂无活跃会话</div>
          ) : (
            <div className="divide-y divide-radar-border/60">
              {sessions.map((s) => (
                <div key={s.id} className="py-3 flex items-center gap-3">
                  <div className="min-w-0 flex-1">
                    <div className="text-xs text-slate-200 flex items-center gap-2">
                      {clientOf(s.user_agent || '')}
                      {s.current && (
                        <span className="chip text-[10px] bg-emerald-500/10 text-emerald-300 border border-emerald-400/20">
                          当前设备
                        </span>
                      )}
                    </div>
                    <div className="text-[11px] text-slate-500 mt-0.5 truncate">
                      {s.ip || 'IP 未知'} · 最近活跃{' '}
                      {s.last_seen_at ? new Date(s.last_seen_at).toLocaleString('zh-CN', { hour12: false }) : '—'}
                    </div>
                  </div>
                  {!s.current && (
                    <button
                      onClick={() => void doRevoke(s.id)}
                      disabled={revoking === s.id}
                      title="吊销该会话"
                      className="p-1.5 rounded-lg text-slate-500 hover:text-red-300 hover:bg-radar-panel disabled:opacity-40 cursor-pointer"
                    >
                      {revoking === s.id ? (
                        <Loader2 className="h-3.5 w-3.5 animate-spin" />
                      ) : (
                        <Trash2 className="h-3.5 w-3.5" />
                      )}
                    </button>
                  )}
                </div>
              ))}
            </div>
          )}
          <div className="mt-4 pt-3 border-t border-radar-border">
            <button
              onClick={() => void doLogoutAll()}
              className="chip border border-radar-border bg-radar-card text-slate-300 hover:text-red-300 hover:border-red-400/40 cursor-pointer"
            >
              <LogOut className="h-3.5 w-3.5" /> 退出登录（当前设备）
            </button>
          </div>
        </section>
      )}
    </div>
  );
}
