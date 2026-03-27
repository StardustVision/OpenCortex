import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { ApiProvider } from './api/Context';
import { App } from './App';
import './index.css';

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <BrowserRouter>
      <ApiProvider>
        <App />
      </ApiProvider>
    </BrowserRouter>
  </React.StrictMode>,
);
