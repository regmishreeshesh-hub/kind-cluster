export enum TaggingOption {
  RANDOM = '1',
  TIMESTAMP = '2',
  CUSTOM = '3'
}

export interface EnvVariable {
  id: string;
  key: string;
  value: string;
  isSecret: boolean;
}

export interface DeploymentConfig {
  repoUrl: string;
  isPublic: boolean;
  ghToken: string;
  branch: string;
  pvcSize: string;
  taggingOption: TaggingOption;
  customTag: string;
  envVars: EnvVariable[];
  dbType: 'postgres' | 'mysql' | 'none';
  clusterName: string;
  createCluster: boolean;
  namespace: string;
}

export const DEFAULT_CONFIG: DeploymentConfig = {
  repoUrl: '',
  isPublic: true,
  ghToken: '',
  branch: 'main',
  pvcSize: '1Gi',
  taggingOption: TaggingOption.RANDOM,
  customTag: 'v1.0.0',
  envVars: [],
  dbType: 'postgres',
  clusterName: 'kind',
  createCluster: false,
  namespace: 'app-deploy'
};