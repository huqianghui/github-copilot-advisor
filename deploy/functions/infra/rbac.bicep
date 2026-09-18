// Adapted from Azure-Samples/functions-quickstart-python-http-azd.
// See ../LICENSE.azure-samples. Role definition paths are subscription-scoped.
param storageAccountName string
param appInsightsName string
param principalId string

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
}

resource insights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: appInsightsName
}

var blobOwner = 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
var queueContributor = '974c5e8b-45b9-4653-ba55-5f855dd0fb88'
var metricsPublisher = '3913510d-42f4-4e42-8a64-420c390055eb'

resource blobRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, principalId, blobOwner)
  scope: storage
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', blobOwner)
  }
}

resource queueRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, principalId, queueContributor)
  scope: storage
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', queueContributor)
  }
}

resource insightsRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(insights.id, principalId, metricsPublisher)
  scope: insights
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', metricsPublisher)
  }
}

output assignmentIds array = [blobRole.id, queueRole.id, insightsRole.id]
