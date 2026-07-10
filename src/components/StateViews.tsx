type StateProps = { message?: string }
export function LoadingState({ message = '正在加载平衡表…' }: StateProps) { return <section className="page-state compact">{message}</section> }
export function EmptyState() { return <section className="page-state compact">暂无数据</section> }
export function ErrorState({ message = '加载失败' }: StateProps) { return <section className="page-state compact">加载失败：{message}</section> }
