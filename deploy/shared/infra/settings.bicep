param identityClientId string
param storageAccountName string
param blobUrl string
param queueUrl string
param openaiEndpoint string
param searchEndpoint string
param modelDeploymentName string
param embeddingDeploymentName string
param searchIndexName string
param runtimeConfigPath string
param runtimeSettings object = {}
@allowed(['entra', 'key'])
param openaiAuthMode string
@allowed(['entra', 'key'])
param searchAuthMode string
param workerParallelism int
param benchmarkEnabled bool
param benchmarkAudience string
param benchmarkApiClientId string
param benchmarkAllowedPrincipalIds array
param benchmarkAllowedClientIds array
param benchmarkReceiverUrl string
param benchmarkReceiverAudience string

output settings object = {
  AZURE_CLIENT_ID: identityClientId
  ADVISOR_ENTRA_CREDENTIAL: 'managed_identity'
  AZURE_OPENAI_AUTH_MODE: openaiAuthMode
  AZURE_SEARCH_AUTH_MODE: searchAuthMode
  ADVISOR_STORAGE_AUTH_MODE: 'entra'
  AZURE_OPENAI_ENDPOINT: openaiEndpoint
  AZURE_SEARCH_ENDPOINT: searchEndpoint
  AZURE_OPENAI_CHAT_DEPLOYMENT: modelDeploymentName
  AZURE_OPENAI_EMBEDDING_DEPLOYMENT: embeddingDeploymentName
  AZURE_SEARCH_INDEX: searchIndexName
  AZURE_OPENAI_API_VERSION: string(runtimeSettings.?AZURE_OPENAI_API_VERSION ?? '2024-10-21')
  KB_SEARCH_BUDGET_SECONDS: string(runtimeSettings.?KB_SEARCH_BUDGET_SECONDS ?? '12')
  GITHUB_SEARCH_BUDGET_SECONDS: string(runtimeSettings.?GITHUB_SEARCH_BUDGET_SECONDS ?? '4')
  WEB_SEARCH_BUDGET_SECONDS: string(runtimeSettings.?WEB_SEARCH_BUDGET_SECONDS ?? '12')
  WEB_SEARCH_ATTEMPT_TIMEOUT_SECONDS: string(runtimeSettings.?WEB_SEARCH_ATTEMPT_TIMEOUT_SECONDS ?? '6')
  ADVISOR_LOG_LEVEL: string(runtimeSettings.?ADVISOR_LOG_LEVEL ?? 'INFO')
  ADVISOR_LOG_FORMAT: string(runtimeSettings.?ADVISOR_LOG_FORMAT ?? 'json')
  ESCALATION_CONFIG: '${runtimeConfigPath}/escalation.yaml'
  DIAGNOSTICS_CONFIG: '${runtimeConfigPath}/diagnostics.yaml'
  ADVISOR_STORAGE_ACCOUNT_NAME: storageAccountName
  ADVISOR_BLOB_SERVICE_URL: blobUrl
  ADVISOR_QUEUE_SERVICE_URL: queueUrl
  ADVISOR_HISTORY_CONTAINER: 'advisor-history'
  ADVISOR_JOB_CONTAINER: 'advisor-jobs'
  ADVISOR_LEASE_CONTAINER: 'advisor-leases'
  ADVISOR_QUEUE_NAME: 'advisor-incoming'
  ADVISOR_POISON_QUEUE_NAME: 'advisor-incoming-poison'
  ADVISOR_WORKER_PARALLELISM: string(workerParallelism)
  ADVISOR_PROCESSING_MODE: 'queued'
  FUNCTIONS_WORKER_PROCESS_COUNT: '1'
  WEB_CONCURRENCY: '1'
  CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID: identityClientId
  CONNECTIONS__SERVICE_CONNECTION__SETTINGS__TENANTID: tenant().tenantId
  CONNECTIONS__SERVICE_CONNECTION__SETTINGS__AUTHTYPE: 'UserManagedIdentity'
  CONNECTIONS__SERVICE_CONNECTION__SETTINGS__SCOPES: 'https://api.botframework.com/.default'
  CONNECTIONS__SERVICE_CONNECTION__SETTINGS__ANONYMOUS_ALLOWED: 'False'
  CONNECTIONS__SERVICE_CONNECTION__SETTINGS__VALIDATE_ISSUER: 'True'
  BENCHMARK_ENABLED: string(benchmarkEnabled)
  BENCHMARK_AUTH_MODE: 'entra'
  BENCHMARK_TENANT_ID: tenant().tenantId
  BENCHMARK_AUDIENCE: benchmarkAudience
  BENCHMARK_API_CLIENT_ID: benchmarkApiClientId
  BENCHMARK_ALLOWED_PRINCIPAL_IDS: join(benchmarkAllowedPrincipalIds, ',')
  BENCHMARK_ALLOWED_CLIENT_IDS: join(benchmarkAllowedClientIds, ',')
  BENCHMARK_REQUIRED_ROLE: 'Benchmark.Invoke'
  BENCHMARK_RECEIVER_URL: benchmarkReceiverUrl
  BENCHMARK_RECEIVER_AUDIENCE: benchmarkReceiverAudience
}
