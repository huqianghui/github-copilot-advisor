targetScope = 'subscription'

@allowed(['southeastasia'])
param location string = 'southeastasia'
param environmentName string = 'copilot-advisor-exp'
param openaiResourceId string
param searchResourceId string
param openaiEndpoint string
param searchEndpoint string
param modelDeploymentName string
param embeddingDeploymentName string
param searchIndexName string
@description('Directory in the package containing the approved escalation.yaml and diagnostics.yaml; never upload .env.')
param runtimeConfigPath string
@description('Non-secret application budgets/logging only; shared settings ignore unrecognized keys.')
param runtimeSettings object = {}
@description('Non-secret package digest/version. OneDeploy uploads the package separately.')
param packageIdentifier string
@allowed(['entra', 'key'])
param openaiAuthMode string = 'entra'
@allowed(['entra', 'key'])
param searchAuthMode string = 'entra'
@secure()
param secretSettings object = {}
@allowed(['3.12'])
param runtimeVersion string = '3.12'
@allowed([512, 2048, 4096])
param instanceMemoryMB int = 512
@description('Explicit scale-out cap; recheck current runtime metadata and regional quota before deployment.')
@minValue(1)
@maxValue(1000)
param maxInstances int = 40
@minValue(1)
@maxValue(32)
param workerParallelism int = 4
param benchmarkEnabled bool = false
param benchmarkAudience string = ''
@description('Precreated Entra API application ID; this template never creates directory objects.')
param benchmarkApiClientId string = ''
param benchmarkAllowedPrincipalIds array = []
param benchmarkAllowedClientIds array = []
param benchmarkReceiverUrl string = ''
param benchmarkReceiverAudience string = ''

var groupName = 'rg-copilot-advisor-flex-exp'
var suffix = uniqueString(subscription().id, groupName, environmentName)
var appName = 'func-advisor-${suffix}'
var tags = { environment: environmentName, experiment: 'flex', workload: 'copilot-advisor' }

module groups '../../shared/infra/resource-groups.bicep' = {
  name: 'advisor-flex-group'
  params: { target: 'flex', location: location, environmentName: environmentName }
}

module identity '../../shared/infra/identity.bicep' = {
  name: 'identity'
  scope: resourceGroup(groupName)
  dependsOn: [groups]
  params: { name: 'id-advisor-${suffix}', location: location, tags: tags }
}

module state '../../shared/infra/state.bicep' = {
  name: 'state'
  scope: resourceGroup(groupName)
  dependsOn: [groups]
  params: {
    name: 'stflex${suffix}'
    location: location
    tags: tags
    includeDeploymentContainer: true
  }
}

module monitoring '../../shared/infra/monitoring.bicep' = {
  name: 'monitoring'
  scope: resourceGroup(groupName)
  dependsOn: [groups]
  params: { name: suffix, location: location, tags: tags, enableApplicationInsights: true }
}

module roles 'rbac.bicep' = {
  name: 'host-roles'
  scope: resourceGroup(groupName)
  params: {
    storageAccountName: state.outputs.name
    appInsightsName: monitoring.outputs.insightsName
    principalId: identity.outputs.principalId
  }
}

module plan 'plan.bicep' = {
  name: 'plan'
  scope: resourceGroup(groupName)
  dependsOn: [groups]
  params: { name: 'plan-advisor-${suffix}', location: location, tags: tags }
}

module openaiRole '../../shared/infra/dependency-roles.bicep' = if (openaiAuthMode == 'entra') {
  name: 'flex-openai-${suffix}'
  scope: resourceGroup(split(openaiResourceId, '/')[2], split(openaiResourceId, '/')[4])
  params: {
    dependency: 'openai'
    resourceName: last(split(openaiResourceId, '/'))
    principalId: identity.outputs.principalId
  }
}

module searchRole '../../shared/infra/dependency-roles.bicep' = if (searchAuthMode == 'entra') {
  name: 'flex-search-${suffix}'
  scope: resourceGroup(split(searchResourceId, '/')[2], split(searchResourceId, '/')[4])
  params: {
    dependency: 'search'
    resourceName: last(split(searchResourceId, '/'))
    principalId: identity.outputs.principalId
  }
}

module settings '../../shared/infra/settings.bicep' = {
  name: 'settings'
  scope: resourceGroup(groupName)
  params: {
    identityClientId: identity.outputs.clientId
    storageAccountName: state.outputs.name
    blobUrl: state.outputs.blobUrl
    queueUrl: state.outputs.queueUrl
    openaiEndpoint: openaiEndpoint
    searchEndpoint: searchEndpoint
    modelDeploymentName: modelDeploymentName
    embeddingDeploymentName: embeddingDeploymentName
    searchIndexName: searchIndexName
    runtimeConfigPath: runtimeConfigPath
    runtimeSettings: runtimeSettings
    openaiAuthMode: openaiAuthMode
    searchAuthMode: searchAuthMode
    workerParallelism: workerParallelism
    benchmarkEnabled: benchmarkEnabled
    benchmarkAudience: benchmarkAudience
    benchmarkApiClientId: benchmarkApiClientId
    benchmarkAllowedPrincipalIds: benchmarkAllowedPrincipalIds
    benchmarkAllowedClientIds: benchmarkAllowedClientIds
    benchmarkReceiverUrl: benchmarkReceiverUrl
    benchmarkReceiverAudience: benchmarkReceiverAudience
  }
}

module app 'api.bicep' = {
  name: 'app'
  scope: resourceGroup(groupName)
  dependsOn: [roles, openaiRole, searchRole]
  params: {
    name: appName
    location: location
    tags: tags
    planId: plan.outputs.id
    identityId: identity.outputs.id
    identityClientId: identity.outputs.clientId
    storageAccountName: state.outputs.name
    appInsightsName: monitoring.outputs.insightsName
    settings: settings.outputs.settings
    secretSettings: secretSettings
    runtimeVersion: runtimeVersion
    instanceMemoryMB: instanceMemoryMB
    maxInstances: maxInstances
    workerParallelism: workerParallelism
    packageIdentifier: packageIdentifier
  }
}

module bot '../../shared/infra/bot.bicep' = {
  name: 'bot'
  scope: resourceGroup(groupName)
  params: {
    name: 'bot-flex-${suffix}'
    tags: tags
    endpoint: '${app.outputs.endpoint}/api/messages'
    identityId: identity.outputs.id
    identityClientId: identity.outputs.clientId
  }
}

output target string = 'functions'
output resource_group string = groupName
output app_id string = app.outputs.id
output app_name string = appName
output endpoint string = app.outputs.endpoint
output storage_id string = state.outputs.id
output blob_url string = state.outputs.blobUrl
output queue_url string = state.outputs.queueUrl
output identity_id string = identity.outputs.id
output identity_client_id string = identity.outputs.clientId
output identity_principal_id string = identity.outputs.principalId
output bot_id string = bot.outputs.id
output created_role_assignment_ids array = concat(
  roles.outputs.assignmentIds,
  openaiAuthMode == 'entra' ? openaiRole!.outputs.assignmentIds : [],
  searchAuthMode == 'entra' ? searchRole!.outputs.assignmentIds : []
)
