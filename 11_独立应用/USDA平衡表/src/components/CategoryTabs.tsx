import type { Category } from '../types/dashboard'

type CategoryTabsProps = { categories: Category[]; selected: Category; onSelect: (category: Category) => void }

export function CategoryTabs({ categories, selected, onSelect }: CategoryTabsProps) {
  return <nav className="category-tabs" aria-label="商品分类">{categories.map((category) => <button className={selected === category ? 'active' : ''} key={category} onClick={() => onSelect(category)} type="button">{category}</button>)}</nav>
}
