// Adapted from the official http-trigger-python-azd template, VNET_ENABLED=false.
// Retain Flex deployment storage + UAMI and AVM 0.15.1; see ../LICENSE.azure-samples.
param name string
param location string
param tags object
param planId string
param identityId string
param identityClientId string
param storageAccountName string
param appInsightsName string
param settings object
@secure()
param secretSettings object
@allowed(['3.12'])
param runtimeVersion string
@allowed([512, 2048, 4096])
param instanceMemoryMB int
@minValue(1)
@maxValue(1000)
param maxInstances int
@minValue(1)
@maxValue(32)
param workerParallelism int
@description('Non-secret artifact digest/version for audit. Package upload uses OneDeploy separately, never a SAS URL.')
param packageIdentifier string

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
}

resource insights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: appInsightsName
}

var hostSettings = {
  BENCHMARK_TARGET_ID: resourceId('Microsoft.Web/sites', name)
  AzureWebJobsStorage__credential: 'managedidentity'
  AzureWebJobsStorage__clientId: identityClientId
  AzureWebJobsStorage__blobServiceUri: storage.properties.primaryEndpoints.blob
  AzureWebJobsStorage__queueServiceUri: storage.properties.primaryEndpoints.queue
  AdvisorQueue__credential: 'managedidentity'
  AdvisorQueue__clientId: identityClientId
  AdvisorQueue__queueServiceUri: storage.properties.primaryEndpoints.queue
  APPLICATIONINSIGHTS_AUTHENTICATION_STRING: 'ClientId=${identityClientId};Authorization=AAD'
  APPLICATIONINSIGHTS_CONNECTION_STRING: insights.properties.ConnectionString
  ADVISOR_MAINTENANCE_SCHEDULE: '0 */5 * * * *'
  ADVISOR_PACKAGE_IDENTIFIER: packageIdentifier
  AzureFunctionsJobHost__extensions__queues__batchSize: string(workerParallelism)
  AzureFunctionsJobHost__extensions__queues__newBatchThreshold: '0'
  AzureFunctionsJobHost__extensions__queues__maxDequeueCount: '5'
  AzureFunctionsJobHost__extensions__queues__messageEncoding: 'none'
  AzureFunctionsJobHost__concurrency__dynamicConcurrencyEnabled: 'false'
  AzureFunctionsJobHost__logging__logLevel__default: 'Warning'
  AzureFunctionsJobHost__logging__logLevel__Function: 'Information'
  'AzureFunctionsJobHost__logging__logLevel__Host.Results': 'Information'
}

var allowedSecretNames = ['AZURE_OPENAI_API_KEY', 'AZURE_SEARCH_API_KEY', 'GITHUB_TOKEN', 'TAVILY_API_KEY', 'BRAVE_API_KEY']
var acceptedSecrets = filter(items(secretSettings), item => contains(allowedSecretNames, item.key) && (item.key != 'AZURE_OPENAI_API_KEY' || settings.AZURE_OPENAI_AUTH_MODE == 'key') && (item.key != 'AZURE_SEARCH_API_KEY' || settings.AZURE_SEARCH_AUTH_MODE == 'key'))

module api 'br/public:avm/res/web/site:0.15.1' = {
  name: 'flex-site'
  params: {
    enableTelemetry: false
    kind: 'functionapp,linux'
    name: name
    location: location
    tags: tags
    serverFarmResourceId: planId
    managedIdentities: {
      systemAssigned: false
      userAssignedResourceIds: [identityId]
    }
    httpsOnly: true
    publicNetworkAccess: 'Enabled'
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${storage.properties.primaryEndpoints.blob}deployment'
          authentication: {
            type: 'UserAssignedIdentity'
            userAssignedIdentityResourceId: identityId
          }
        }
      }
      scaleAndConcurrency: {
        instanceMemoryMB: instanceMemoryMB
        maximumInstanceCount: maxInstances
        alwaysReady: []
        triggers: {
          http: {
            perInstanceConcurrency: workerParallelism
          }
        }
      }
      runtime: {
        name: 'python'
        version: runtimeVersion
      }
    }
    siteConfig: {
      alwaysOn: false
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      scmMinTlsVersion: '1.2'
      http20Enabled: true
    }
    virtualNetworkSubnetId: null
    appSettingsKeyValuePairs: union(
      toObject(acceptedSecrets, item => item.key, item => string(item.value)),
      settings,
      hostSettings
    )
  }
}

output id string = api.outputs.resourceId
output endpoint string = 'https://${api.outputs.defaultHostname}'
