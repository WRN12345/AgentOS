import { useEffect, useRef, useState, type ReactNode } from "react";

import { cn } from "@/lib/utils";

/**
 * 轻量下拉菜单：useState + onClick + 绝对定位，不依赖 Radix Popper/Portal。
 *
 * 背景：顶栏的 Radix DropdownMenu 在部分运行环境（内网 http://IP + 特定浏览器）
 * 下 pointer 交互无响应，而页面上普通按钮的 onClick 均正常；顶栏两个菜单
 * （通知、账号）改用此实现以保证任何环境可点击。弹层交互：点击外部 / Escape 关闭。
 */
interface SimpleMenuProps {
  /** 渲染触发器：把 toggle 接到触发按钮的 onClick 上。 */
  trigger: (toggle: () => void, open: boolean) => ReactNode;
  /** 菜单内容；传函数可拿到 close() 以便点击菜单项后关闭。 */
  children: ReactNode | ((close: () => void) => ReactNode);
  /** 弹层附加样式（如宽度）。 */
  contentClassName?: string;
}

export function SimpleMenu({ trigger, children, contentClassName }: SimpleMenuProps) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) {
      return;
    }
    const onPointerDown = (event: PointerEvent) => {
      if (ref.current && !ref.current.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setOpen(false);
      }
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  return (
    <div ref={ref} className="relative">
      {trigger(() => setOpen((v) => !v), open)}
      {open && (
        <div
          role="menu"
          className={cn(
            "absolute right-0 top-full z-50 mt-1 rounded-lg bg-popover p-1 text-popover-foreground shadow-md ring-1 ring-foreground/10",
            contentClassName,
          )}
        >
          {typeof children === "function" ? children(() => setOpen(false)) : children}
        </div>
      )}
    </div>
  );
}

/** 菜单项统一样式（与 shadcn dropdown-menu-item 视觉一致）。 */
export const simpleMenuItemClass =
  "flex w-full cursor-pointer items-center gap-1.5 rounded-md px-1.5 py-1 text-sm hover:bg-accent";
