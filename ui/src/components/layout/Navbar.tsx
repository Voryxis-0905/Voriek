import React, { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import { CodeBracketIcon, GlobeAltIcon } from '@heroicons/react/24/outline';
import { api } from '../../api/client';

interface NavbarProps {
  currentWorld?: string;
  onWorldChange?: (world: string) => void;
}

export const Navbar: React.FC<NavbarProps> = ({ currentWorld, onWorldChange }) => {
  const location = useLocation();
  const navigate = useNavigate();
  const [worlds, setWorlds] = useState<string[]>([]);
  const headerRef = useRef<HTMLElement>(null);

  useEffect(() => {
    loadWorlds();
  }, []);

  // Publish the navbar's real height as `--navbar-h`.
  //
  // The play screen pins its mobile drawer and icon strip to the bottom of the
  // viewport, and they have to start *below* this navbar: it is sticky and
  // ~137px tall on a phone, so anything anchored at `top-0` has its first
  // controls covered and untappable. The height depends on how the nav items
  // wrap, so it is measured rather than hardcoded - a magic number would drift
  // the moment the nav content changes.
  useLayoutEffect(() => {
    const el = headerRef.current;
    if (!el) return;
    const publish = () => {
      document.documentElement.style.setProperty('--navbar-h', `${el.getBoundingClientRect().height}px`);
    };
    publish();
    const observer = new ResizeObserver(publish);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const loadWorlds = async () => {
    try {
      const list = await api.worlds.list();
      setWorlds(list.map(w => w.name));
    } catch {
      // ignore
    }
  };

  const isActive = (path: string) => {
    if (path === '/' && location.pathname === '/') return true;
    if (path !== '/' && location.pathname.startsWith(path)) return true;
    return false;
  };

  return (
    <header ref={headerRef} className="sticky top-0 z-50 px-8 py-4 flex items-center justify-between glass-panel shadow-sm">
      {/* Left: Brand Logo & Title */}
      <div className="flex items-center gap-6">
        <Link to="/" className="flex items-center gap-3 group text-decoration-none">
          <div className="w-10 h-10 rounded-full flex items-center justify-center shadow-md bg-[conic-gradient(from_210deg,var(--sakura),var(--periwinkle),var(--gold),var(--sakura))] text-white group-hover:scale-105 transition-transform">
            <GlobeAltIcon className="w-5 h-5 text-white drop-shadow-sm" />
          </div>
          <div className="flex flex-col">
            <span className="font-[var(--font-display)] font-bold text-xl text-[var(--ink-main)] tracking-tight group-hover:text-[var(--periwinkle-dark)] transition-colors">
              Voriek
            </span>
            <span className="text-[10.5px] tracking-[0.07em] text-[var(--ink-soft)] uppercase font-semibold">
              Interactive storytelling · local
            </span>
          </div>
        </Link>
      </div>

      {/* Right Navigation & Tools */}
      <div className="flex items-center gap-5 text-sm font-medium">
        <Link
          to="/"
          className={`transition-colors ${isActive('/') ? 'text-[var(--accent-sage)] font-bold' : 'text-[var(--ink-soft)] hover:text-[var(--ink-main)]'}`}
        >
          Docs
        </Link>

        <Link
          to="/worlds"
          className={`transition-colors ${isActive('/worlds') && !location.pathname.includes('/play') ? 'text-[var(--accent-sage)] font-bold' : 'text-[var(--ink-soft)] hover:text-[var(--ink-main)]'}`}
        >
          Worlds
        </Link>

        <Link
          to="/settings"
          className={`transition-colors ${isActive('/settings') ? 'text-[var(--accent-sage)] font-bold' : 'text-[var(--ink-soft)] hover:text-[var(--ink-main)]'}`}
        >
          Settings
        </Link>

        {/* World Quick Selector */}
        {worlds.length > 0 && (
          <div className="flex items-center gap-1.5 bg-[var(--bg-subtle)] px-3 py-1.5 rounded-lg border border-[var(--line)] text-xs transition-colors hover:bg-[var(--line)]">
            <GlobeAltIcon  className="w-5 h-5 text-[var(--accent-sage)]" />
            <select
              value={currentWorld || ''}
              onChange={(e) => {
                const selected = e.target.value;
                if (onWorldChange) onWorldChange(selected);
                if (selected) navigate(`/worlds/${selected}/play`);
              }}
              className="bg-transparent font-medium text-xs text-[var(--ink-main)] focus:outline-none cursor-pointer pr-1"
            >
              {worlds.map(w => (
                <option key={w} value={w} className="text-[var(--ink-main)]">{w}</option>
              ))}
            </select>
          </div>
        )}

        {/* Version Pill */}
        <span className="px-3 py-1 rounded-full bg-[var(--bg-subtle)] text-xs text-[var(--ink-soft)] font-mono font-bold border border-[var(--line)] shadow-sm">
          v0.11.3
        </span>

        {/* GitHub Link */}
        <a
          href="https://github.com/Voryxis-0905/Voriek"
          target="_blank"
          rel="noreferrer"
          aria-label="Voriek on GitHub"
          title="Voriek on GitHub"
          className="text-[var(--ink-soft)] hover:text-[var(--ink-main)] transition-colors"
        >
          <CodeBracketIcon className="w-5 h-5" />
        </a>
      </div>
    </header>
  );
};
