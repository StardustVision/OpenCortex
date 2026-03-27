import React, { useState, useEffect } from 'react';
import { Search } from 'lucide-react';
import { useDebounce } from '../../hooks/useDebounce';

interface SearchInputProps {
  onSearch: (query: string) => void;
  placeholder?: string;
  className?: string;
}

export const SearchInput: React.FC<SearchInputProps> = ({ onSearch, placeholder = 'Search...', className = '' }) => {
  const [value, setValue] = useState('');
  const debouncedValue = useDebounce(value, 300);

  useEffect(() => {
    onSearch(debouncedValue);
  }, [debouncedValue, onSearch]);

  return (
    <div className={`relative ${className}`}>
      <Search className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" size={18} />
      <input
        type="text"
        className="w-full pl-10 pr-4 py-2 bg-white border border-gray-200 rounded-md focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent text-sm transition-all"
        placeholder={placeholder}
        value={value}
        onChange={(e) => setValue(e.target.value)}
      />
    </div>
  );
};
