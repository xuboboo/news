import { Navigate, Route, Routes } from 'react-router-dom';
import Layout from './components/Layout';
import ErrorBoundary from './components/ErrorBoundary';
import HotBoard from './pages/HotBoard';
import Latest from './pages/Latest';
import Events from './pages/Events';
import EventDetail from './pages/EventDetail';
import DailyReport from './pages/DailyReport';
import Admin from './pages/Admin';
import Usage from './pages/Usage';
import NotFound from './pages/NotFound';

/** 页面级错误边界：单页崩溃不拖垮全局布局 */
function bound(page: string, element: React.ReactNode) {
  return <ErrorBoundary page={page}>{element}</ErrorBoundary>;
}

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route path="/" element={<Navigate to="/hot" replace />} />
        <Route path="/hot" element={bound('热榜中心', <HotBoard />)} />
        <Route path="/latest" element={bound('最新资讯', <Latest />)} />
        <Route path="/events" element={bound('热点事件', <Events />)} />
        <Route path="/events/:id" element={bound('事件详情', <EventDetail />)} />
        <Route path="/report" element={bound('科技日报', <DailyReport />)} />
        <Route path="/usage" element={bound('成本看板', <Usage />)} />
        <Route path="/admin" element={bound('运维监控', <Admin />)} />
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  );
}
