import React from 'react';
import ReactDOM from 'react-dom/client';
import { createHashRouter, RouterProvider } from 'react-router-dom';
import App from './App';
import { startBootstrap, apiMode, startAutoRefresh } from './api/client';
import './theme'; // 渲染前应用主题（兜底 index.html pre-paint 脚本未生效的场景）
import './index.css';

async function start() {
  // 立即渲染首屏（骨架态），数据在后台引导，避免白屏等待后端
  const router = createHashRouter([{ path: '*', element: <App /> }]);
  ReactDOM.createRoot(document.getElementById('root')!).render(
    <React.StrictMode>
      <RouterProvider router={router} />
    </React.StrictMode>
  );

  await startBootstrap();

  if (!apiMode) {
    console.warn('[华军软件园资讯雷达系统] 后端不可达，已降级为本地数据引擎');
  } else {
    // API 模式下启动周期性增量刷新（60s），页面自动吃到新数据
    startAutoRefresh(60000);
  }
}

void start();
