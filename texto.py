Preciso de implementar na aplicação Web (HTTPS) uma funcionalidade equivalente ao antigo "Copiar Template", mas totalmente adaptada à arquitetura Web moderna.

ANÁLISE DA VERSÃO ANTIGA

Na versão desktop existia uma funcionalidade "Copiar Template" baseada na tabela:

file_templates
- template_id
- name
- category
- rel_path
- enabled
- description

Os templates eram armazenados numa pasta central e quando o utilizador selecionava um template, o sistema copiava o ficheiro para a pasta da tarefa através da função copy_template_to_task_folder(...). A lógica funcionava bem e pretendo manter o conceito, mas adaptado à arquitetura Web/HTTPS.

OBJETIVO

Permitir que o utilizador selecione um template e o associe à tarefa atual, criando uma cópia independente do documento.

REQUISITOS PRINCIPAIS

1. Não utilizar paths locais
- Não utilizar C:
- Não utilizar caminhos de rede
- Não utilizar OneDrive local
- Não depender do sistema de ficheiros do servidor

Tudo deverá ser armazenado e gerido via SQL Server e APIs.

2. Tabela de Templates

Criar ou adaptar a tabela para armazenar os templates diretamente na base de dados:

CREATE TABLE dbo.file_templates (
    template_id NVARCHAR(128) PRIMARY KEY,
    name NVARCHAR(255) NOT NULL,
    category NVARCHAR(100) NULL,
    description NVARCHAR(1000) NULL,

    original_filename NVARCHAR(255) NOT NULL,
    content_type NVARCHAR(255) NULL,

    file_data VARBINARY(MAX) NOT NULL,

    enabled BIT NOT NULL DEFAULT(1),

    created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
    created_by NVARCHAR(255) NULL
);

3. Ficheiros das tarefas

Caso ainda não exista, criar uma tabela para guardar os ficheiros associados às tarefas:

CREATE TABLE dbo.task_files (
    id INT IDENTITY PRIMARY KEY,

    task_id NVARCHAR(100) NOT NULL,

    file_name NVARCHAR(255) NOT NULL,
    file_extension NVARCHAR(20) NULL,
    content_type NVARCHAR(255) NULL,

    file_data VARBINARY(MAX) NOT NULL,

    uploaded_by NVARCHAR(255) NULL,
    uploaded_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
);

4. Funcionalidade "Usar Template"

Adicionar novo botão na tarefa:

[Usar Template]

Ao clicar deve abrir um modal de seleção.

5. Grelha de Templates

Mostrar:

- Nome
- Categoria
- Descrição

Filtros:

- Pesquisa
- Categoria
- Apenas ativos

6. Processo de cópia

Quando o utilizador selecionar um template:

Exemplo:
8D Report.docx

O sistema deverá:

1. Ler o template da tabela file_templates
2. Criar um novo registo em task_files
3. Associar o novo ficheiro à tarefa atual
4. Manter sempre o template original intacto

Fluxo:

Template
↓
Duplicar
↓
Ficheiro da tarefa

Nunca editar o template original.

7. Nome do ficheiro

Permitir:

- Utilizar nome original
ou
- Renomear antes de criar

Exemplos:

8D Report.docx

ou

Task_123_8D_Report.docx

8. Gestão de anexos

Após copiar o template, o ficheiro deve aparecer automaticamente na área de anexos da tarefa.

Opções disponíveis:

- Abrir
- Download
- Eliminar

9. Administração de Templates

Criar nova área:

Administração → Templates

Funcionalidades:

- Adicionar Template
- Editar Template
- Editar Descrição
- Ativar
- Desativar
- Eliminar

10. Permissões

Apenas:
- Admin
- Editor

podem gerir templates.

Todos os utilizadores podem utilizar templates ativos.

11. APIs

Implementar endpoints semelhantes a:

GET     /api/templates
GET     /api/templates/{id}
POST    /api/templates
PUT     /api/templates/{id}
DELETE  /api/templates/{id}

POST    /api/tasks/{taskId}/copy-template/{templateId}

12. Experiência de Utilização

Na secção de anexos da tarefa pretendo algo deste género:

📎 Anexos

[Adicionar Ficheiro]
[Usar Template]

-----------------------

8D Report.docx
Control Plan.xlsx
Lessons Learned.docx

O botão "Usar Template" deverá abrir diretamente o seletor.

13. Compatibilidade Obrigatória

A implementação deve:

- Funcionar em HTTPS
- Funcionar na Web
- Ser multiutilizador
- Guardar tudo em SQL Server
- Não depender de paths locais
- Não depender de OneDrive local
- Não depender do sistema de ficheiros local
- Ser compatível com a futura integração SharePoint
- Ser compatível com futura integração Azure Blob Storage
- Não obrigar a alterações no frontend caso o armazenamento físico mude futuramente

14. Melhoria adicional pretendida

Analisar a possibilidade de criar "Pacotes de Templates".

Exemplos:

Pacote APQP
- Control Plan
- PFMEA
- Process Flow
- Lessons Learned

Pacote 8D
- 8D Report
- Containment
- Verification Plan

Pacote Auditoria
- Checklist
- Plano de Ações
- Relatório

Ao selecionar um pacote, todos os documentos são copiados automaticamente para a tarefa numa única operação.

Antes de implementar, analisar impacto na arquitetura atual, compatibilidade com as restantes funcionalidades da aplicação, segurança, permissões, performance, base de dados e futura manutenção.