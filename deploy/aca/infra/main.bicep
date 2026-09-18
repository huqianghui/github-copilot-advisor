targetScope = 'subscription'

@allowed(['southeastasia'])
param location string = 'southeastasia'
param environmentName string = 'copilot-advisor-exp'
@description('Existing Cognitive Services account ARM ID; no model resources are created.')
param openaiResourceId string
@description('Existing Search service ARM ID; only index-data-reader access is assigned.')
param searchResourceId string
param openaiEndpoint string
param searchEndpoint string
param modelDeploymentName string
param embeddingDeploymentName string
param searchIndexName string
@description('Directory in the image containing the approved escalation.yaml and diagnostics.yaml; never upload .env.')
param runtimeConfigPath string
@description('Repository:tag or repository@sha256:digest; required when deployApp=true.')
param image string = ''
@description('Bootstrap with false, push the image to the NEW registry, then redeploy with true.')
param deployApp bool = false
@allowed(['entra', 'key'])
param openaiAuthMode string = 'entra'
@allowed(['entra', 'key'])
param searchAuthMode string = 'entra'
@secure()
@description('Explicit allowlisted runtime secrets only. Protected settings and unused Azure keys are ignored.')
param secretSettings object = {}
@allowed([0, 1])
param minReplicas int = 0
@description('Explicit CPU/memory pair: quarter=0.25/0.5Gi, half=0.5/1Gi, one=1/2Gi, two=2/4Gi. Never auto-upsize; resample after changing.')
@allowed(['quarter', 'half', 'one', 'two'])
param resourceProfile string = 'quarter'
@minValue(1)
@maxValue(1000)
param maxInstances int = 40
@minValue(1)
@maxValue(32)
param workerParallelism int = 4
param benchmarkEnabled bool = false
param benchmarkAudience string = ''
@description('Precreated Entra application ID; registration/consent is a separate prerequisite.')
param benchmarkApiClientId string = ''
param benchmarkAllowedPrincipalIds array = []
param benchmarkAllowedClientIds array = []
param benchmarkReceiverUrl string = ''
param benchmarkReceiverAudience string = ''

var groupName = 'rg-copilot-advisor-aca-exp'
var suffix = uniqueString(subscription().id, groupName, environmentName)
var appName = 'ca-advisor-${suffix}'
var tags = { environment: environmentName, experiment: 'aca', workload: 'copilot-advisor' }

module groups '../../shared/infra/resource-groups.bicep' = {
  name: 'advisor-aca-group'
  params: { target: 'aca', location: location, environmentName: environmentName }
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
  params: { name: 'staca${suffix}', location: location, tags: tags }
}

module stateRoles '../../shared/infra/state-roles.bicep' = {
  name: 'state-roles'
  scope: resourceGroup(groupName)
  params: { storageAccountName: state.outputs.name, principalId: identity.outputs.principalId }
}

module monitoring '../../shared/infra/monitoring.bicep' = {
  name: 'monitoring'
  scope: resourceGroup(groupName)
  dependsOn: [groups]
  params: { name: suffix, location: location, tags: tags }
}

module registry 'registry.bicep' = {
  name: 'registry'
  scope: resourceGroup(groupName)
  params: {
    name: 'cradvisor${suffix}'
    location: location
    tags: tags
    principalId: identity.outputs.principalId
  }
}

module environment 'environment.bicep' = {
  name: 'environment'
  scope: resourceGroup(groupName)
  params: {
    name: 'cae-advisor-${suffix}'
    location: location
    tags: tags
    workspaceName: monitoring.outputs.workspaceName
  }
}

module openaiRole '../../shared/infra/dependency-roles.bicep' = if (openaiAuthMode == 'entra') {
  name: 'aca-openai-${suffix}'
  scope: resourceGroup(split(openaiResourceId, '/')[2], split(openaiResourceId, '/')[4])
  params: {
    dependency: 'openai'
    resourceName: last(split(openaiResourceId, '/'))
    principalId: identity.outputs.principalId
  }
}

module searchRole '../../shared/infra/dependency-roles.bicep' = if (searchAuthMode == 'entra') {
  name: 'aca-search-${suffix}'
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

module app 'container-app.bicep' = if (deployApp) {
  name: 'app'
  scope: resourceGroup(groupName)
  dependsOn: [stateRoles, openaiRole, searchRole]
  params: {
    name: appName
    location: location
    tags: tags
    environmentId: environment.outputs.id
    identityId: identity.outputs.id
    registryServer: registry.outputs.server
    image: image
    storageAccountName: state.outputs.name
    settings: settings.outputs.settings
    secretSettings: secretSettings
    minReplicas: minReplicas
    resourceProfile: resourceProfile
    maxInstances: maxInstances
    workerParallelism: workerParallelism
  }
}

module bot '../../shared/infra/bot.bicep' = if (deployApp) {
  name: 'bot'
  scope: resourceGroup(groupName)
  params: {
    name: 'bot-aca-${suffix}'
    tags: tags
    endpoint: '${app!.outputs.endpoint}/api/messages'
    identityId: identity.outputs.id
    identityClientId: identity.outputs.clientId
  }
}

output target string = 'aca'
output resource_group string = groupName
output app_id string = deployApp ? app!.outputs.id : ''
output app_name string = deployApp ? appName : ''
output endpoint string = deployApp ? app!.outputs.endpoint : ''
output storage_id string = state.outputs.id
output blob_url string = state.outputs.blobUrl
output queue_url string = state.outputs.queueUrl
output identity_id string = identity.outputs.id
output identity_client_id string = identity.outputs.clientId
output identity_principal_id string = identity.outputs.principalId
output bot_id string = deployApp ? bot!.outputs.id : ''
output created_role_assignment_ids array = concat(
  stateRoles.outputs.assignmentIds,
  registry.outputs.assignmentIds,
  openaiAuthMode == 'entra' ? openaiRole!.outputs.assignmentIds : [],
  searchAuthMode == 'entra' ? searchRole!.outputs.assignmentIds : []
)
