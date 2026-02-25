import React, { useState } from 'react';
import { Icons } from './Icon';

interface TerminalPreviewProps {
  scriptContent: string;
}

const TerminalPreview: React.FC<TerminalPreviewProps> = ({ scriptContent }) => {
  const [copied, setCopied] = useState(false);

  const handleCopy = () => {
    navigator.clipboard.writeText(scriptContent);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const handleDownload = () => {
    const blob = new Blob([scriptContent], { type: 'text/x-sh' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'deploy.sh';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  return (
    <div className="flex flex-col h-full bg-[#1e1e1e] rounded-lg border border-slate-700 overflow-hidden shadow-2xl">
      <div className="flex items-center justify-between px-4 py-2 bg-[#2d2d2d] border-b border-black/20">
        <div className="flex items-center gap-2">
          <Icons.Terminal size={16} className="text-slate-400" />
          <span className="text-sm font-mono text-slate-300">deploy.sh</span>
        </div>
        <div className="flex items-center gap-2">
          <button 
            onClick={handleCopy}
            className="p-1.5 hover:bg-slate-700 rounded transition-colors text-slate-400 hover:text-white"
            title="Copy to clipboard"
          >
            {copied ? <Icons.CheckCheck size={16} className="text-green-400" /> : <Icons.Copy size={16} />}
          </button>
        </div>
      </div>
      <div className="flex-1 overflow-auto p-4 font-mono text-xs md:text-sm leading-relaxed text-terminal-text">
        <pre className="whitespace-pre-wrap break-all">
          <code dangerouslySetInnerHTML={{ 
            __html: scriptContent
              .replace(/#.*/g, '<span class="text-terminal-green">$&</span>')
              .replace(/(set|echo|read|if|fi|then|else|case|esac|for|done|while|do|in|function|local|return|exit)/g, '<span class="text-terminal-blue">$&</span>')
              .replace(/(\${.*?}|".*?")/g, '<span class="text-terminal-yellow">$&</span>')
          }} />
        </pre>
      </div>
      <div className="p-4 border-t border-slate-700 bg-[#252526]">
        <button
          onClick={handleDownload}
          className="w-full flex items-center justify-center gap-2 bg-blue-600 hover:bg-blue-500 text-white py-2 px-4 rounded font-medium transition-all active:scale-95"
        >
          <Icons.Play size={18} />
          Download Script
        </button>
      </div>
    </div>
  );
};

export default TerminalPreview;