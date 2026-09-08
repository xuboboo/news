export type Channel = '软件之家' | 'Win11之家' | 'Win10之家' | '智能时代';

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
  /** 对行业变化的描述（用于科技日报） */
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
