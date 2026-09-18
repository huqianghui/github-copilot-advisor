@description('Deploy only into the ACA experiment group. Nothing is created until explicitly enabled.')
param enabled bool = false
@description('Second phase only: bootstrap the identity/container/roles before building an image with the real driver client ID.')
param deployApp bool = false
@allowed(['southeastasia'])
param location string = 'southeastasia'
param environmentName string = 'copilot-advisor-bench'
@description('Existing experiment ACA environment; one combined driver/receiver replica is separate from the target app.')
param environmentId string
@description('The NEW Basic registry created by the ACA experiment, in this resource group.')
param registryName string
@description('Image running the driver and HTTP callback receiver in one process, using the same monotonic clock.')
param image string = ''
@description('Existing ACA experiment state account in this resource group; no third storage account is created.')
param storageAccountName string
@description('One precreated single-tenant Entra API registration with Benchmark.Invoke; no Graph objects or app-role grants are created.')
@minLength(36)
@maxLength(36)
param apiClientId string
@minLength(1)
param audience string
param operatorPrincipalIds array = []
param operatorClientIds array = []
@minLength(1)
param workloadPrincipalIds array
@minLength(1)
param workloadClientIds array
param targetEndpoint string

var suffix = uniqueString(resourceGroup().id, environmentName)
var appName = 'ca-bench-${suffix}'
var tags = {
  environment: environmentName
  experiment: 'benchmark'
  workload: 'copilot-advisor'
  temporary: 'true'
}
resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: registryName
}

resource existingEnvironment 'Microsoft.App/managedEnvironments@2025-01-01' existing = {
  name: last(split(environmentId, '/'))
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = if (enabled) {
  name: 'id-bench-${suffix}'
  location: location
  tags: tags
}

var acrPull = '7f951dda-4ed3-4680-a7ca-43fe172d538d'
resource pull 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (enabled) {
  name: guid(registry.id, identity.id, acrPull)
  scope: registry
  properties: {
    principalId: identity!.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPull)
  }
}

resource results 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
}

resource blobs 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' existing = {
  parent: results
  name: 'default'
}

resource raw 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = if (enabled) {
  parent: blobs
  name: 'benchmark-results'
  properties: { publicAccess: 'None' }
}

var blobContributor = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
resource resultsRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (enabled) {
  name: guid(raw.id, identity.id, blobContributor)
  scope: raw
  properties: {
    principalId: identity!.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', blobContributor)
  }
}

resource app 'Microsoft.App/containerApps@2025-01-01' = if (enabled && deployApp) {
  name: appName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  dependsOn: [pull, resultsRole]
  properties: {
    environmentId: environmentId
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: { external: true, allowInsecure: false, targetPort: 8080, transport: 'http' }
      registries: [{ server: registry.properties.loginServer, identity: identity.id }]
    }
    template: {
      containers: [
        {
          name: 'driver-receiver'
          image: '${registry.properties.loginServer}/${image}'
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: [
            { name: 'PORT', value: '8080' }
            { name: 'WEB_CONCURRENCY', value: '1' }
            { name: 'AZURE_CLIENT_ID', value: identity!.properties.clientId }
            { name: 'ADVISOR_ENTRA_CREDENTIAL', value: 'managed_identity' }
            { name: 'ADVISOR_STORAGE_AUTH_MODE', value: 'entra' }
            { name: 'ADVISOR_BLOB_SERVICE_URL', value: results.properties.primaryEndpoints.blob }
            { name: 'BENCHMARK_RESULTS_CONTAINER', value: 'benchmark-results' }
            { name: 'BENCHMARK_ENABLED', value: 'true' }
            { name: 'BENCHMARK_AUTH_MODE', value: 'entra' }
            { name: 'BENCHMARK_TENANT_ID', value: tenant().tenantId }
            { name: 'BENCHMARK_AUDIENCE', value: audience }
            { name: 'BENCHMARK_OPERATOR_PRINCIPAL_IDS', value: join(operatorPrincipalIds, ',') }
            { name: 'BENCHMARK_OPERATOR_CLIENT_IDS', value: join(operatorClientIds, ',') }
            { name: 'BENCHMARK_WORKLOAD_PRINCIPAL_IDS', value: join(workloadPrincipalIds, ',') }
            { name: 'BENCHMARK_WORKLOAD_CLIENT_IDS', value: join(workloadClientIds, ',') }
            { name: 'BENCHMARK_REQUIRED_ROLE', value: 'Benchmark.Invoke' }
            { name: 'BENCHMARK_TARGET_ENDPOINT', value: targetEndpoint }
            { name: 'BENCHMARK_TARGET_AUDIENCE', value: audience }
            { name: 'BENCHMARK_AUTOSTART', value: 'false' }
          ]
        }
      ]
      scale: { minReplicas: 1, maxReplicas: 1 }
    }
  }
}

// Platform auth admits both callers; application route validators must separate
// operator control requests from workload callbacks using the two allowlists.
resource auth 'Microsoft.App/containerApps/authConfigs@2025-01-01' = if (enabled && deployApp) {
  parent: app
  name: 'current'
  properties: {
    platform: { enabled: true }
    globalValidation: { unauthenticatedClientAction: 'Return401' }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          clientId: apiClientId
          openIdIssuer: '${environment().authentication.loginEndpoint}${tenant().tenantId}/v2.0'
        }
        validation: {
          allowedAudiences: [audience]
          defaultAuthorizationPolicy: {
            allowedApplications: union(operatorClientIds, workloadClientIds)
            allowedPrincipals: { identities: union(operatorPrincipalIds, workloadPrincipalIds) }
          }
        }
      }
    }
    httpSettings: { requireHttps: true }
  }
}

output planned_app_id string = enabled ? resourceId('Microsoft.App/containerApps', appName) : ''
output planned_app_name string = enabled ? appName : ''
output planned_endpoint string = enabled ? 'https://${appName}.${existingEnvironment.properties.defaultDomain}' : ''
output receiver_endpoint string = enabled && deployApp ? 'https://${app!.properties.configuration.ingress!.fqdn}' : ''
output driver_endpoint string = enabled && deployApp ? 'https://${app!.properties.configuration.ingress!.fqdn}' : ''
output results_storage_id string = enabled ? results.id : ''
output results_container_id string = enabled ? raw!.id : ''
output identity_id string = enabled ? identity!.id : ''
output identity_client_id string = enabled ? identity!.properties.clientId : ''
output identity_principal_id string = enabled ? identity!.properties.principalId : ''
output created_role_assignment_ids array = enabled ? [pull!.id, resultsRole!.id] : []
