"use client";

import Link from "next/link";

import { navigationItems, type AppScreen } from "./navigation";

interface MobileNavProps {
  activeScreen: AppScreen;
  onMore: () => void;
}

const primaryIds: AppScreen[] = ["today", "transactions", "accounts", "categories"];

export function MobileNav({ activeScreen, onMore }: MobileNavProps) {
  const items = navigationItems.filter((item) => primaryIds.includes(item.id));
  return <nav aria-label="Мобильная навигация" className="shell-mobile-nav">{items.map((item) => <Link className={activeScreen === item.id ? "is-active" : ""} href={item.href} key={item.id}><span>{item.icon}</span><small>{item.label}</small></Link>)}<button onClick={onMore} type="button"><span className="mobile-more-icon">•••</span><small>Ещё</small></button></nav>;
}
