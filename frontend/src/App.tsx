import { Navigate, Route, Routes } from 'react-router-dom';
import Layout from './components/Layout';
import RequireAuth from './components/RequireAuth';
import ErrorBoundary from './components/ErrorBoundary';
import HotBoard from './pages/HotBoard';
import Latest from './pages/Latest';
import Events from './pages/Events';
import EventDetail from './pages/EventDetail';
import DailyReport from './pages/DailyReport';
import Admin from './pages/Admin';
import Usage from './pages/Usage';
import Login from './pages/Login';
import Account from './pages/Account';
import Users from './pages/Users';
import Audit from './pages/Audit';
import NotFound from './pages/NotFound';

/** 页面级错误边界：单页崩溃不拖垮全局布局。
 *  key=page：路由切换时强制重建边界实例，崩溃态不残留到下一个页面。 */
function bound(page: string, element: React.ReactNode) {
  return (
    <ErrorBoundary key={page} page={page}>
      {element}
    </ErrorBoundary>
  );
}

export default function App() {
  return (
    <Routes>
      {/* 登录页独立于主布局（无侧边栏/顶栏） */}
      <Route path="/login" element={bound('登录', <Login />)} />

      {/* 全站强制登录：RequireAuth 未登录跳 /login，首登强制改密只放行 /account */}
      <Route element={<RequireAuth />}>
        <Route element={<Layout />}>
          <Route path="/" element={<Navigate to="/hot" replace />} />
          <Route path="/hot" element={bound('热榜中心', <HotBoard />)} />
          <Route path="/latest" element={bound('最新资讯', <Latest />)} />
          <Route path="/events" element={bound('热点事件', <Events />)} />
          <Route path="/events/:id" element={bound('事件详情', <EventDetail />)} />
          <Route path="/report" element={bound('科技日报', <DailyReport />)} />
          <Route path="/account" element={bound('账号设置', <Account />)} />
          <Route path="/users" element={bound('用户与租户', <Users />)} />
          <Route path="/audit" element={bound('审计日志', <Audit />)} />
          <Route path="/usage" element={bound('成本看板', <Usage />)} />
          <Route path="/admin" element={bound('运维监控', <Admin />)} />
          <Route path="*" element={<NotFound />} />
        </Route>
      </Route>
    </Routes>
  );
}
