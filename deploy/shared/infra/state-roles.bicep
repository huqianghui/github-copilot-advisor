param storageAccountName string
param principalId string

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
}

var roleIds = [
  'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
  '974c5e8b-45b9-4653-ba55-5f855dd0fb88'
]

resource roles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for roleId in roleIds: {
  name: guid(storage.id, principalId, roleId)
  scope: storage
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleId)
  }
}]

output assignmentIds array = [for (roleId, index) in roleIds: roles[index].id]
