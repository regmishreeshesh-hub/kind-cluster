import React, { useState, useEffect } from 'react';
import { DeploymentConfig, DEFAULT_CONFIG, TaggingOption, EnvVariable } from './types';
import { generateBashScript } from './utils/scriptGenerator';
import TerminalPreview from './components/TerminalPreview';
import { Icons } from './components/Icon';

const STEPS = [
  { id: 1, title: 'Repository', icon: Icons.GitBranch },
  { id: 2, title: 'Analysis & Env', icon: Icons.Search },
  { id: 3, title: 'Build Config', icon: Icons.Box },
  { id: 4, title: 'Cluster', icon: Icons.Server },
];

const App: React.FC = () => {
  const [config, setConfig] = useState<DeploymentConfig>(DEFAULT_CONFIG);
  const [script, setScript] = useState<string>('');
  const [currentStep, setCurrentStep] = useState(1);
  
  // Simulation States
  const [isScanning, setIsScanning] = useState(false);
  const [branches, setBranches] = useState<string[]>([]);
  const [detectedFiles, setDetectedFiles] = useState<string[]>([]);
  const [detectedClusters, setDetectedClusters] = useState<string[]>([]);

  useEffect(() => {
    setScript(generateBashScript(config));
  }, [config]);

  // Sync Namespace with Repo Name if not manually edited (simple heuristic)
  useEffect(() => {
    if (config.repoUrl && config.namespace === 'app-deploy') {
        const repoName = config.repoUrl.split('/').pop()?.replace('.git', '').toLowerCase() || 'app-deploy';
        // Only auto-update if it looks like the default
        updateConfig('namespace', repoName);
    }
  }, [config.repoUrl]);

  const updateConfig = (key: keyof DeploymentConfig, value: any) => {
    setConfig(prev => ({ ...prev, [key]: value }));
  };

  const nextStep = () => setCurrentStep(p => Math.min(p + 1, STEPS.length));
  const prevStep = () => setCurrentStep(p => Math.max(p - 1, 1));

  // --- Step 1 Actions ---
  const handleScanBranches = () => {
    if (!config.repoUrl) return;
    setIsScanning(true);
    // Simulate API delay
    setTimeout(() => {
      setBranches(['main', 'develop', 'feature/auth-v2', 'staging']);
      if (!config.branch) updateConfig('branch', 'main');
      setIsScanning(false);
    }, 1500);
  };

  // --- Step 2 Actions ---
  const handleAnalyzeSource = () => {
    setIsScanning(true);
    setTimeout(() => {
      setDetectedFiles(['./Dockerfile', './backend/Dockerfile', '.env.example']);
      // Simulate discovering env vars
      if (config.envVars.length === 0) {
        updateConfig('envVars', [
          { id: '1', key: 'PORT', value: '8080', isSecret: false },
          { id: '2', key: 'DATABASE_URL', value: '', isSecret: true }
        ]);
      }
      setIsScanning(false);
    }, 2000);
  };

  // --- Step 4 Actions ---
  const handleScanClusters = () => {
    setIsScanning(true);
    setTimeout(() => {
      setDetectedClusters(['kind', 'minikube', 'docker-desktop']);
      setIsScanning(false);
    }, 1500);
  };

  const addEnvVar = () => {
    const newVar: EnvVariable = {
      id: Math.random().toString(36).substr(2, 9),
      key: '',
      value: '',
      isSecret: false
    };
    updateConfig('envVars', [...config.envVars, newVar]);
  };

  const updateEnvVar = (id: string, field: keyof EnvVariable, value: any) => {
    const newEnvVars = config.envVars.map(ev => 
      ev.id === id ? { ...ev, [field]: value } : ev
    );
    updateConfig('envVars', newEnvVars);
  };

  const removeEnvVar = (id: string) => {
    updateConfig('envVars', config.envVars.filter(ev => ev.id !== id));
  };

  // --- Renderers ---

  const renderStepIndicator = () => (
    <div className="flex justify-between items-center mb-8 px-2 relative">
      <div className="absolute left-0 top-1/2 -translate-y-1/2 w-full h-0.5 bg-slate-800 -z-10" />
      {STEPS.map((s) => {
        const isActive = s.id === currentStep;
        const isCompleted = s.id < currentStep;
        return (
          <div key={s.id} className="flex flex-col items-center gap-2 bg-slate-900 px-2">
            <div 
              className={`w-8 h-8 rounded-full flex items-center justify-center transition-all duration-300 border-2 ${
                isActive ? 'border-blue-500 bg-blue-500/20 text-blue-400 shadow-[0_0_15px_rgba(59,130,246,0.5)]' : 
                isCompleted ? 'border-emerald-500 bg-emerald-500/20 text-emerald-400' : 
                'border-slate-700 bg-slate-800 text-slate-500'
              }`}
            >
              {isCompleted ? <Icons.CheckCheck size={14} /> : <s.icon size={14} />}
            </div>
            <span className={`text-[10px] font-medium uppercase tracking-wider ${isActive ? 'text-blue-400' : 'text-slate-500'}`}>
              {s.title}
            </span>
          </div>
        );
      })}
    </div>
  );

  const renderStepContent = () => {
    switch (currentStep) {
      case 1:
        return (
          <div className="space-y-6 animate-in fade-in slide-in-from-right-4 duration-300">
            <div className="bg-slate-800/50 p-5 rounded-xl border border-slate-700/50 space-y-4">
              <h3 className="text-lg font-medium flex items-center gap-2 text-slate-200">
                <Icons.Globe className="text-blue-400" size={18} /> Connect Repository
              </h3>
              
              <div className="space-y-3">
                <label className="text-sm text-slate-400">Git Repository URL</label>
                <div className="flex gap-2">
                  <input
                    type="text"
                    placeholder="https://github.com/username/repo.git"
                    value={config.repoUrl}
                    onChange={(e) => updateConfig('repoUrl', e.target.value)}
                    className="flex-1 bg-slate-900 border border-slate-700 rounded-lg px-4 py-2.5 text-sm focus:ring-2 focus:ring-blue-500 outline-none font-mono"
                  />
                </div>
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="text-sm text-slate-400 block mb-1.5">Visibility</label>
                  <select
                    value={config.isPublic ? 'public' : 'private'}
                    onChange={(e) => updateConfig('isPublic', e.target.value === 'public')}
                    className="w-full bg-slate-900 border border-slate-700 rounded-lg px-4 py-2.5 text-sm focus:ring-2 focus:ring-blue-500 outline-none"
                  >
                    <option value="public">Public</option>
                    <option value="private">Private</option>
                  </select>
                </div>
                {!config.isPublic && (
                  <div>
                    <label className="text-sm text-yellow-500 block mb-1.5">Personal Access Token</label>
                    <input
                      type="password"
                      placeholder="ghp_..."
                      value={config.ghToken}
                      onChange={(e) => updateConfig('ghToken', e.target.value)}
                      className="w-full bg-slate-900 border border-yellow-800/50 rounded-lg px-4 py-2.5 text-sm focus:ring-2 focus:ring-yellow-500 outline-none"
                    />
                  </div>
                )}
              </div>

              <div className="pt-2">
                <button
                  onClick={handleScanBranches}
                  disabled={!config.repoUrl || isScanning}
                  className="w-full flex items-center justify-center gap-2 bg-slate-700 hover:bg-slate-600 disabled:opacity-50 text-white py-2.5 rounded-lg transition-all text-sm font-medium"
                >
                  {isScanning ? <Icons.Loader2 className="animate-spin" size={16} /> : <Icons.RefreshCw size={16} />}
                  {isScanning ? 'Scanning...' : 'Scan for Branches'}
                </button>
              </div>

              {branches.length > 0 && (
                <div className="space-y-2 animate-in fade-in slide-in-from-top-2">
                   <label className="text-sm text-green-400 block">Select Branch</label>
                   <div className="grid grid-cols-2 gap-2">
                     {branches.map(b => (
                       <button
                         key={b}
                         onClick={() => updateConfig('branch', b)}
                         className={`text-sm px-3 py-2 rounded-lg text-left transition-all border ${
                           config.branch === b 
                            ? 'bg-green-500/10 border-green-500/50 text-green-400' 
                            : 'bg-slate-900 border-slate-700 text-slate-400 hover:border-slate-500'
                         }`}
                       >
                         <Icons.GitBranch className="inline mr-2" size={12}/>
                         {b}
                       </button>
                     ))}
                   </div>
                </div>
              )}
            </div>
          </div>
        );

      case 2:
        return (
          <div className="space-y-6 animate-in fade-in slide-in-from-right-4 duration-300">
            {detectedFiles.length === 0 ? (
               <div className="bg-slate-800/50 p-8 rounded-xl border border-slate-700/50 text-center space-y-4">
                 <div className="w-16 h-16 bg-slate-700/50 rounded-full flex items-center justify-center mx-auto text-slate-400">
                   <Icons.Search size={32} />
                 </div>
                 <div>
                   <h3 className="text-lg font-medium text-slate-200">Analyze Source Code</h3>
                   <p className="text-slate-400 text-sm mt-1">We'll scan for Dockerfiles and .env configurations.</p>
                 </div>
                 <button
                  onClick={handleAnalyzeSource}
                  disabled={isScanning}
                  className="px-6 py-2 bg-blue-600 hover:bg-blue-500 text-white rounded-lg font-medium transition-all flex items-center gap-2 mx-auto"
                >
                  {isScanning ? <Icons.Loader2 className="animate-spin" size={16} /> : <Icons.Play size={16} />}
                  Start Analysis
                </button>
               </div>
            ) : (
              <div className="space-y-4">
                 <div className="bg-slate-800/50 p-4 rounded-xl border border-slate-700/50">
                    <h4 className="text-xs font-semibold uppercase text-slate-500 tracking-wider mb-3">Detected Files</h4>
                    <div className="flex flex-wrap gap-2">
                      {detectedFiles.map(f => (
                        <span key={f} className="inline-flex items-center gap-1.5 px-3 py-1 bg-slate-900 rounded-full text-xs text-slate-300 border border-slate-700">
                          <Icons.FileCode size={12} className="text-blue-400" /> {f}
                        </span>
                      ))}
                    </div>
                 </div>

                 <div className="bg-slate-800/50 p-4 rounded-xl border border-slate-700/50 space-y-4">
                   <div className="flex justify-between items-center">
                     <h4 className="text-xs font-semibold uppercase text-slate-500 tracking-wider">Environment Variables</h4>
                     <button onClick={addEnvVar} className="text-xs bg-slate-700 hover:bg-slate-600 text-white px-2 py-1 rounded">
                       <Icons.Plus size={12} /> Add
                     </button>
                   </div>
                   <div className="space-y-2 max-h-48 overflow-y-auto pr-2 custom-scrollbar">
                     {config.envVars.map((ev) => (
                        <div key={ev.id} className="flex gap-2 items-start group">
                          <input
                            placeholder="KEY"
                            value={ev.key}
                            onChange={(e) => updateEnvVar(ev.id, 'key', e.target.value)}
                            className="w-1/3 bg-slate-900 border border-slate-700 rounded px-2 py-1.5 text-xs focus:ring-1 focus:ring-blue-500 outline-none font-mono"
                          />
                          <input
                            placeholder="VALUE"
                            value={ev.value}
                            onChange={(e) => updateEnvVar(ev.id, 'value', e.target.value)}
                            className="flex-1 bg-slate-900 border border-slate-700 rounded px-2 py-1.5 text-xs focus:ring-1 focus:ring-blue-500 outline-none font-mono"
                          />
                          <button 
                            onClick={() => updateEnvVar(ev.id, 'isSecret', !ev.isSecret)}
                            className={`px-2 py-1.5 rounded text-xs transition-colors ${ev.isSecret ? 'bg-yellow-500/10 text-yellow-500 border border-yellow-500/20' : 'bg-slate-700 text-slate-400'}`}
                          >
                            Secret
                          </button>
                          <button onClick={() => removeEnvVar(ev.id)} className="p-1.5 text-slate-500 hover:text-red-400 opacity-0 group-hover:opacity-100 transition-opacity">
                            <Icons.Trash2 size={14} />
                          </button>
                        </div>
                      ))}
                   </div>
                 </div>

                 <div className="bg-slate-800/50 p-4 rounded-xl border border-slate-700/50">
                    <label className="text-xs font-semibold uppercase text-slate-500 tracking-wider block mb-2">Database Type</label>
                    <div className="grid grid-cols-3 gap-2">
                      {['postgres', 'mysql', 'none'].map((type) => (
                        <button
                          key={type}
                          onClick={() => updateConfig('dbType', type)}
                          className={`px-3 py-2 text-sm rounded-lg border capitalize transition-all ${
                            config.dbType === type
                            ? 'bg-purple-500/10 border-purple-500/50 text-purple-400'
                            : 'bg-slate-900 border-slate-700 text-slate-400 hover:border-slate-500'
                          }`}
                        >
                          {type}
                        </button>
                      ))}
                    </div>
                 </div>
              </div>
            )}
          </div>
        );

      case 3:
        return (
          <div className="space-y-6 animate-in fade-in slide-in-from-right-4 duration-300">
             <div className="bg-slate-800/50 p-5 rounded-xl border border-slate-700/50 space-y-4">
                <h3 className="text-lg font-medium text-slate-200">Deployment Config</h3>

                <div>
                   <label className="text-sm text-slate-400 block mb-1.5">Image Tagging Strategy</label>
                   <div className="grid grid-cols-1 gap-2">
                     <select
                        value={config.taggingOption}
                        onChange={(e) => updateConfig('taggingOption', e.target.value)}
                        className="w-full bg-slate-900 border border-slate-700 rounded-lg px-4 py-2.5 text-sm focus:ring-2 focus:ring-blue-500 outline-none"
                      >
                        <option value={TaggingOption.RANDOM}>Random Suffix (e.g. v1.0.0-ax92s)</option>
                        <option value={TaggingOption.TIMESTAMP}>Unix Timestamp (e.g. v162524...)</option>
                        <option value={TaggingOption.CUSTOM}>Static / Manual Tag</option>
                      </select>
                   </div>
                </div>

                {config.taggingOption === TaggingOption.CUSTOM && (
                  <div className="animate-in fade-in slide-in-from-top-1">
                     <label className="text-sm text-slate-400 block mb-1.5">Custom Tag</label>
                     <input
                        type="text"
                        value={config.customTag}
                        onChange={(e) => updateConfig('customTag', e.target.value)}
                        className="w-full bg-slate-900 border border-slate-700 rounded-lg px-4 py-2.5 text-sm focus:ring-2 focus:ring-blue-500 outline-none font-mono"
                      />
                  </div>
                )}

                <div className="border-t border-slate-700/50 pt-4">
                   <label className="text-sm text-slate-400 block mb-1.5">Target Namespace</label>
                   <div className="flex items-center gap-2">
                      <Icons.Box size={16} className="text-slate-500" />
                      <input
                        type="text"
                        value={config.namespace}
                        onChange={(e) => updateConfig('namespace', e.target.value)}
                        className="flex-1 bg-slate-900 border border-slate-700 rounded-lg px-4 py-2.5 text-sm focus:ring-2 focus:ring-blue-500 outline-none font-mono"
                        placeholder="default"
                      />
                   </div>
                   <p className="text-xs text-slate-500 mt-2">
                     Defaults to the repository name if left blank.
                   </p>
                </div>

                <div>
                   <label className="text-sm text-slate-400 block mb-1.5">PVC Storage Size</label>
                   <input
                      type="text"
                      value={config.pvcSize}
                      onChange={(e) => updateConfig('pvcSize', e.target.value)}
                      className="w-full bg-slate-900 border border-slate-700 rounded-lg px-4 py-2.5 text-sm focus:ring-2 focus:ring-blue-500 outline-none"
                    />
                </div>
             </div>
          </div>
        );

      case 4:
        return (
          <div className="space-y-6 animate-in fade-in slide-in-from-right-4 duration-300">
             <div className="bg-slate-800/50 p-5 rounded-xl border border-slate-700/50 space-y-4">
                <h3 className="text-lg font-medium text-slate-200">Cluster Target</h3>
                
                <div className="flex gap-4">
                   <button
                     onClick={handleScanClusters}
                     disabled={isScanning}
                     className="flex-1 flex items-center justify-center gap-2 bg-slate-700 hover:bg-slate-600 text-white py-3 rounded-lg transition-all text-sm font-medium"
                   >
                     {isScanning ? <Icons.Loader2 className="animate-spin" size={16} /> : <Icons.RefreshCw size={16} />}
                     Scan Local Clusters
                   </button>
                </div>

                {detectedClusters.length > 0 && (
                   <div className="space-y-2 animate-in fade-in slide-in-from-top-2">
                      <p className="text-xs text-slate-400 uppercase font-semibold tracking-wider">Detected Contexts</p>
                      {detectedClusters.map(c => (
                        <button
                          key={c}
                          onClick={() => {
                            updateConfig('clusterName', c);
                            updateConfig('createCluster', false);
                          }}
                          className={`w-full flex items-center justify-between px-4 py-3 rounded-lg border transition-all ${
                            config.clusterName === c && !config.createCluster
                              ? 'bg-purple-500/10 border-purple-500/50 text-purple-400'
                              : 'bg-slate-900 border-slate-700 text-slate-300 hover:border-slate-500'
                          }`}
                        >
                          <span className="flex items-center gap-2">
                            <Icons.Server size={16} /> {c}
                          </span>
                          {config.clusterName === c && !config.createCluster && <Icons.CheckCheck size={16} />}
                        </button>
                      ))}
                   </div>
                )}

                <div className="relative py-2">
                  <div className="absolute inset-0 flex items-center"><div className="w-full border-t border-slate-700"></div></div>
                  <div className="relative flex justify-center"><span className="bg-slate-800 px-2 text-xs text-slate-500">OR</span></div>
                </div>

                <button
                  onClick={() => updateConfig('createCluster', true)}
                  className={`w-full flex items-center justify-between px-4 py-3 rounded-lg border transition-all ${
                    config.createCluster
                      ? 'bg-blue-500/10 border-blue-500/50 text-blue-400'
                      : 'bg-slate-900 border-slate-700 text-slate-300 hover:border-slate-500'
                  }`}
                >
                  <span className="flex items-center gap-2">
                    <Icons.Plus size={16} /> Create New Kind Cluster
                  </span>
                  {config.createCluster && <Icons.CheckCheck size={16} />}
                </button>

                {config.createCluster && (
                  <div className="animate-in fade-in slide-in-from-top-1">
                    <label className="text-sm text-slate-400 block mb-1.5">New Cluster Name</label>
                    <input
                      type="text"
                      value={config.clusterName}
                      onChange={(e) => updateConfig('clusterName', e.target.value)}
                      className="w-full bg-slate-900 border border-slate-700 rounded-lg px-4 py-2.5 text-sm focus:ring-2 focus:ring-blue-500 outline-none"
                    />
                  </div>
                )}

             </div>
          </div>
        );
      
      default:
        return null;
    }
  };

  return (
    <div className="min-h-screen bg-slate-900 text-slate-100 flex flex-col md:flex-row overflow-hidden">
      
      {/* Sidebar / Configuration Form */}
      <div className="w-full md:w-1/2 lg:w-5/12 h-screen flex flex-col border-r border-slate-800">
        
        {/* Header */}
        <div className="p-6 border-b border-slate-800">
            <h1 className="text-xl font-bold bg-clip-text text-transparent bg-gradient-to-r from-blue-400 to-emerald-400">
              K8s Deployer
            </h1>
            <p className="text-slate-500 text-xs mt-1">
              Interactive Configuration Wizard
            </p>
        </div>

        {/* Scrollable Wizard Content */}
        <div className="flex-1 overflow-y-auto p-6 scrollbar-thin">
           {renderStepIndicator()}
           {renderStepContent()}
        </div>

        {/* Footer Navigation */}
        <div className="p-4 border-t border-slate-800 bg-slate-900/50 backdrop-blur-sm flex justify-between items-center">
            <button
              onClick={prevStep}
              disabled={currentStep === 1}
              className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
                currentStep === 1 ? 'text-slate-600 cursor-not-allowed' : 'text-slate-300 hover:bg-slate-800'
              }`}
            >
              <Icons.ChevronLeft size={16} /> Back
            </button>

            {currentStep < 4 ? (
               <button
                 onClick={nextStep}
                 className="flex items-center gap-2 px-6 py-2 bg-blue-600 hover:bg-blue-500 text-white rounded-lg text-sm font-medium shadow-lg shadow-blue-500/20 transition-all active:scale-95"
               >
                 Next <Icons.ChevronRight size={16} />
               </button>
            ) : (
              <div className="text-green-400 text-sm font-medium flex items-center gap-2 animate-pulse">
                <Icons.CheckCheck size={16} /> Ready
              </div>
            )}
        </div>
      </div>

      {/* Main Content / Terminal Preview */}
      <div className="flex-1 bg-black p-4 md:p-8 flex flex-col h-screen overflow-hidden">
         <div className="mb-4 hidden md:block">
            <h2 className="text-lg font-medium text-slate-300">Live Script Preview</h2>
            <p className="text-slate-500 text-sm">
              {currentStep === 1 && "Start by configuring your source repository..."}
              {currentStep === 2 && "Reviewing environment configuration..."}
              {currentStep === 3 && "Configuring build and deployment parameters..."}
              {currentStep === 4 && "Finalizing cluster and execution logic..."}
            </p>
         </div>
        <div className="flex-1 min-h-0">
          <TerminalPreview scriptContent={script} />
        </div>
      </div>
    </div>
  );
};

export default App;