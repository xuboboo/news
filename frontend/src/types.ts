export type Channel =
  | '软件之家'
  | 'Win11之家'
  | 'Win10之家'
  | '智能时代'
  /** 快科技（文章页面包屑二级栏目 = 软件之家大类） */
  | '桌面系统'
  | '电脑软件'
  | '移动应用'
  | '手机系统';

export type EventStatus = '发酵中' | '持续关注' | '已平息';

export interface ArticleSummary {
  /** 一句话摘要 */
  summary_short: string;
  /** 核心信息（3-5 条） */
  key_points: string[];
  /** 标签 */
  tags: string[];
  /** 生成模型 */
  model: string;
}

export interface Article {
  id: string;
  title: string;
  channel: Channel;
  source: string;
  /** 源站原文链接（API 模式有值；本地演示数据无） */
  url?: string;
  /** 距今多少分钟前发布 */
  publishOffsetMin: number;
  views: number;
  comments: number;
  shares: number;
  /** 点赞（IT之家「有价值」打分数，crawler/stats.py 轮询） */
  likes: number;
  summary: ArticleSummary;
  /** 关联热点事件 ID（后端 event_ids 全量关联，数字串；缺省 = 旧快照数据） */
  eventIds?: string[];
  /** AI 处理状态（后端 ai_status）：pending 处理中 / summarized 已总结 / clustered 已聚类 */
  aiStatus?: string;
  /** 对行业变化的描述（用于软件日报） */
  industryChange?: string;
}

export interface TimelineEntry {
  /** 事件节点发生时刻（绝对时间，显示时实时计算相对值） */
  time: Date;
  text: string;
  source?: string;
}

export interface HotEvent {
  id: string;
  title: string;
  /** AI 总结 */
  summary: string;
  /** 核心影响 */
  impacts: string[];
  articleIds: string[];
  status: EventStatus;
  timeline: TimelineEntry[];
}

export interface ArticleWithScore extends Article {
  publishTime: Date;
  rawScore: number;
  hotScore: number;
}
