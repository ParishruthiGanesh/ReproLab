"use client";

import { usePathname } from "next/navigation";
import Link from "next/link";

const LINKS = [
  { href: "/", label: "New experiment" },
  { href: "/runs", label: "Run history" },
  { href: "/example", label: "Example report" },
  { href: "/about", label: "How it works" },
];

export function Nav() {
  const pathname = usePathname();
  return (
    <nav className="nav">
      {LINKS.map((link) => {
        const active =
          link.href === "/" ? pathname === "/" : pathname.startsWith(link.href);
        return (
          <Link
            key={link.href}
            href={link.href}
            aria-current={active ? "page" : undefined}
          >
            {link.label}
          </Link>
        );
      })}
    </nav>
  );
}
