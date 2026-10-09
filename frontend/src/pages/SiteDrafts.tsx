/** 独立路由/接口，复用已经验证过的列表分页和异步保护。 */
import Drafts from './Drafts';

export default function SiteDrafts() {
  return <Drafts channel="site" />;
}
