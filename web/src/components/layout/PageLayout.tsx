import React from 'react';
import { Sidebar } from './Sidebar';
import { RefreshCw } from 'lucide-react';
import { Button } from '../common/Button';

interface PageLayoutProps {
  children: React.ReactNode;
  title: string;
  onRefresh?: () => void;
  isLoading?: boolean;
}

export const PageLayout: React.FC<PageLayoutProps> = ({ children, title, onRefresh, isLoading }) => {
  return (
    <div className="flex w-full bg-gray-50 min-h-screen">
      <Sidebar />
      <main className="flex-1 flex flex-col min-w-0">
        <header className="sticky top-0 z-10 bg-white border-b border-gray-200 px-8 h-[64px] flex items-center justify-between">
          <h1 className="text-xl font-bold text-gray-900">{title}</h1>
          {onRefresh && (
            <Button 
              variant="ghost" 
              size="sm" 
              onClick={onRefresh} 
              loading={isLoading}
              className="text-gray-500 hover:text-indigo-600"
            >
              <RefreshCw size={18} className={isLoading ? 'animate-spin' : ''} />
            </Button>
          )}
        </header>
        <div className="p-8">
          {children}
        </div>
      </main>
    </div>
  );
};
