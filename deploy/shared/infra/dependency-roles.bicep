@description('Module scope must be the dependency resource group, not the experiment group.')
@allowed(['openai', 'search'])
param dependency string
param resourceName string
param principalId string

resource openai 'Microsoft.CognitiveServices/accounts@2024-10-01' existing = if (dependency == 'openai') {
  name: resourceName
}

resource search 'Microsoft.Search/searchServices@2023-11-01' existing = if (dependency == 'search') {
  name: resourceName
}

var openaiRoleId = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
var searchRoleId = '1407120a-92aa-4202-b7e9-c0e197c71c8f'

resource openaiRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (dependency == 'openai') {
  name: guid(openai.id, principalId, openaiRoleId)
  scope: openai
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', openaiRoleId)
  }
}

resource searchRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (dependency == 'search') {
  name: guid(search.id, principalId, searchRoleId)
  scope: search
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', searchRoleId)
  }
}

output assignmentIds array = dependency == 'openai' ? [openaiRole!.id] : [searchRole!.id]
