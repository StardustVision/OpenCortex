import React, { useState, useEffect } from 'react';
import { NavLink } from 'react-router-dom';
import { 
  LayoutDashboard, 
  Brain, 
  BookOpen, 
  SearchCode, 
  Settings,
  Sparkles,
  Key,
  ChevronLeft,
  ChevronRight,
  LogOut
} from 'lucide-react';
import { useApi } from '../../api/Context';

interface NavItem {
  icon: React.FC<any>;
  label: string;
  path: string;
  status: string;
  adminOnly?: boolean;
}

const navItems: NavItem[] = [
  { icon: LayoutDashboard, label: 'Dashboard', path: '/', status: 'active' },
  { icon: Brain, label: 'Memories', path: '/memories', status: 'active' },
  { icon: BookOpen, label: 'Knowledge', path: '/knowledge', status: 'active' },
  { icon: SearchCode, label: 'Search Debug', path: '/search-debug', status: 'active' },
  { icon: Settings, label: 'System', path: '/system', status: 'active' },
  { icon: Key, label: 'Tokens', path: '/tokens', status: 'active', adminOnly: true },
  { icon: Sparkles, label: 'Skills', path: '/skills', status: 'coming-soon' },
];

export const Sidebar: React.FC = () => {
  const [isExpanded, setIsExpanded] = useState(() => {
    const saved = localStorage.getItem('sidebar_expanded');
    return saved !== null ? JSON.parse(saved) : true;
  });
  const { logout, role } = useApi();

  useEffect(() => {
    localStorage.setItem('sidebar_expanded', JSON.stringify(isExpanded));
  }, [isExpanded]);

  return (
    <aside 
      className={`flex flex-col bg-white border-r border-gray-200 transition-all duration-300 h-screen sticky top-0 ${
        isExpanded ? 'w-[220px]' : 'w-[64px]'
      }`}
    >
      <div className="flex items-center justify-between p-4 mb-4">
        {isExpanded && <span className="font-bold text-indigo-600 truncate">OpenCortex</span>}
        <button 
          onClick={() => setIsExpanded(!isExpanded)}
          className="p-1 rounded-md hover:bg-gray-100 text-gray-400 mx-auto"
        >
          {isExpanded ? <ChevronLeft size={20} /> : <ChevronRight size={20} />}
        </button>
      </div>

      <nav className="flex-1 px-2 space-y-1">
        {navItems.filter(item => !item.adminOnly || role === 'admin').map((item) => (
          <NavLink
            key={item.path}
            to={item.path}
            className={({ isActive }) => `
              flex items-center gap-3 px-3 py-2 rounded-md transition-colors
              ${item.status === 'coming-soon' ? 'opacity-50 cursor-not-allowed pointer-events-none' : ''}
              ${isActive && item.status !== 'coming-soon'
                ? 'bg-indigo-50 text-indigo-600' 
                : 'text-gray-600 hover:bg-gray-50 hover:text-gray-900'}
            `}
            title={item.status === 'coming-soon' ? 'Coming Soon' : item.label}
          >
            <item.icon size={20} className="shrink-0" />
            {isExpanded && <span className="text-sm font-medium">{item.label}</span>}
          </NavLink>
        ))}
      </nav>

      <div className="p-2 border-t border-gray-100">
        <button 
          onClick={logout}
          className="flex items-center gap-3 w-full px-3 py-2 rounded-md text-gray-600 hover:bg-red-50 hover:text-red-600 transition-colors"
        >
          <LogOut size={20} className="shrink-0" />
          {isExpanded && <span className="text-sm font-medium">Logout</span>}
        </button>
      </div>
    </aside>
  );
};
