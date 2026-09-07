import { Link } from 'react-router-dom';
import { Radar } from 'lucide-react';
import { usePageTitle } from '../utils/hooks';

export default function NotFound() {
  usePageTitle('页面不存在');
  return (
    <div className="card py-20 text-center max-w-md mx-auto mt-10">
      <div className="inline-flex p-3 rounded-xl bg-accent/10 text-accent mb-4">
        <Radar className="h-6 w-6" />
      </div>
      <div className="text-lg font-semibold text-slate-100 mb-1.5">页面不存在</div>
      <div className="text-sm text-slate-500 mb-6">你要访问的页面可能已被移动或删除。</div>
      <Link
        to="/hot"
        className="chip bg-accent/15 text-accent border border-accent/40 px-4 py-2 hover:bg-accent/25"
      >
        返回热榜中心
      </Link>
    </div>
  );
}
